"""One entry point for every model call.

    data = await llm.json("actor", system, user)                 # -> dict
    data = await llm.json("vision", system, user, image=jpeg)    # -> dict (image = raw bytes)

Each role maps to an ordered fallback chain of (provider, model). Within a model we rotate over the
provider's API keys; rate-limited keys are cooled down, models that keep failing are skipped for a while.
"""
import asyncio
import base64
import itertools
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from openai import AsyncOpenAI

from backend.config import BASE_URLS, KEYS


class LLMError(Exception):
    pass


@dataclass
class Model:
    provider: str            # groq | nvidia | gemini | openai
    name: str
    tries: int = 2           # attempts (each on the next key) before falling through to the next model
    timeout: float = 45
    json_mode: bool = False  # send response_format=json_object
    extra: dict = field(default_factory=dict)


GPT_OSS = dict(reasoning_effort="low")
# Nemotron reasoning models spend the whole token budget "thinking" on long prompts and return no JSON; thinking off = ~3s, reliable
NO_THINK = dict(extra_body={"chat_template_kwargs": {"enable_thinking": False}})

# Paid OpenAI models (measured on our stuck-navigation benchmark, Oct 2026: gpt-6-luna 4/4 on unlabelled controls at
# ~5.5k in / 250 out tokens = $0.0007 per call; text-only gpt-oss 1/4). Used only where free models fall short, under a per-run cap.
LUNA = Model("openai", "gpt-6-luna", tries=2, timeout=30, json_mode=True, extra=dict(reasoning_effort="low"))
SOL = Model("openai", "gpt-6.1-sol", tries=1, timeout=45, json_mode=True, extra=dict(reasoning_effort="low"))

CHAINS: dict[str, list[Model]] = {
    # persona brain: fast, per-step. Groq's free tier allows ~one 5k-token prompt per minute per key, so with three
    # personas it 429s constantly; Luna answers then instead of the much weaker gpt-oss-20b
    "actor": [
        Model("groq", "openai/gpt-oss-120b", tries=3, timeout=30, json_mode=True, extra=GPT_OSS),
        LUNA,
        Model("groq", "openai/gpt-oss-20b", tries=2, timeout=30, json_mode=True, extra=GPT_OSS),
        Model("nvidia", "nvidia/nemotron-3-ultra-550b-a55b", tries=2, timeout=60, extra=NO_THINK),
    ],
    # once per run: persona goals, integrity judge, final report. Nemotron Ultra measured 9-42s and echoed
    # the schema once, so it is the quality fallback rather than the primary.
    "planner": [
        Model("groq", "openai/gpt-oss-120b", tries=3, timeout=40, json_mode=True, extra=dict(reasoning_effort="medium")),
        Model("nvidia", "nvidia/nemotron-3-ultra-550b-a55b", tries=2, timeout=75, extra=NO_THINK),
        Model("groq", "openai/gpt-oss-20b", tries=2, timeout=40, json_mode=True, extra=GPT_OSS),
    ],
    # looks at screenshots (perceiver + set-of-marks grounder): Gemma first, Nemotron Nano Omni as backup
    # Gemma's hosted API fails ~half the calls with a fast 500 under load, so Nano Omni comes right after it
    "vision": [
        Model("gemini", "gemma-4-26b-a4b-it", tries=3, timeout=25),
        Model("nvidia", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", tries=2, timeout=40,
              extra=NO_THINK),   # 2s instead of 30s
        Model("gemini", "gemma-4-31b-it", tries=1, timeout=25),
    ],
}
# the Deal-Hunter deliberately tries abuses; measured: gpt-6-luna stops after one ("I'll stop rather than submit additional
# payment-manipulation attempts"), so the integrity persona keeps the open-weight chain, which works through every idea
CHAINS["actor_integrity"] = [m for m in CHAINS["actor"] if m.provider != "openai"]
CHAINS["reporter"] = CHAINS["planner"]
CHAINS["grounder"] = CHAINS["vision"]
# takes over a step when navigation went wrong: sees the numbered (Set-of-Marks) screenshot and decides in character
CHAINS["navigator"] = [LUNA, SOL]


class KeyPool:
    """Round-robin over keys, skipping ones that are cooling down."""

    def __init__(self, keys: list[str]):
        self.keys = keys
        self._cycle = itertools.cycle(range(len(keys))) if keys else None
        self.cool_until = [0.0] * len(keys)

    def next(self) -> tuple[int, str]:
        if not self.keys:
            raise LLMError("no API keys configured for provider")
        now = time.time()
        for _ in range(len(self.keys)):
            i = next(self._cycle)
            if self.cool_until[i] <= now:
                return i, self.keys[i]
        i = min(range(len(self.keys)), key=lambda j: self.cool_until[j])  # all cooling: take the soonest
        return i, self.keys[i]

    def cool(self, i: int, seconds: float):
        self.cool_until[i] = max(self.cool_until[i], time.time() + seconds)


POOLS = {p: KeyPool(k) for p, k in KEYS.items()}
# price per 1M tokens: (input, cached input, output); published rates via third-party listings, Oct 2026
PRICES = {"gpt-6-luna": (0.10, 0.01, 0.50), "gpt-6.1-sol": (2.00, 0.10, 10.00)}
PAID = {"openai"}
# paid calls still allowed in the current run (set by the runner); `reserve` of them is kept for the vision navigator,
# so rate-limit fallbacks for the actor can never use up the budget meant for rescuing a stuck persona
BUDGET = {"calls_left": 0, "reserve": 0}


def paid_available(role: str = "navigator") -> bool:
    floor = 0 if role == "navigator" else BUDGET["reserve"]
    return bool(POOLS["openai"].keys) and BUDGET["calls_left"] > floor


def cost_usd(model_key: str, u: dict) -> float:
    p = PRICES.get(model_key.split("/", 1)[-1])
    if not p:
        return 0.0
    return ((u["in"] - u["cached"]) * p[0] + u["cached"] * p[1] + u["out"] * p[2]) / 1e6
STATS: dict[str, dict] = {}                       # "provider/model" -> calls/fails/secs
USAGE: dict[str, dict] = {}                       # "provider/model" -> tokens in / cached / out (what the API reported)
ERRORS: list[str] = []                            # last failures, for debugging
_streak: dict[str, int] = {}                      # consecutive failures per model
_bad_until: dict[str, float] = {}                 # circuit breaker
_clients: dict[tuple, AsyncOpenAI] = {}
_http = httpx.AsyncClient(timeout=90)


def _stat(model: Model, ok: bool, secs: float, err: str = ""):
    s = STATS.setdefault(f"{model.provider}/{model.name}", {"calls": 0, "fails": 0, "secs": 0.0, "errors": {}})
    s["calls"] += 1
    s["fails"] += 0 if ok else 1
    s["secs"] += secs
    if err:
        s["errors"][err] = s["errors"].get(err, 0) + 1


# ---------------------------------------------------------------- JSON extraction
def extract_json(text: str) -> dict:
    """Pull the first JSON object out of model text (handles <thought> blocks, ``` fences, trailing commas)."""
    if not text:
        raise LLMError("empty response")
    text = re.sub(r"<(thought|think|thinking)>.*?</\1>", "", text, flags=re.S | re.I)
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = [fence.group(1)] if fence else []
    start = text.find("{")
    while start != -1 and len(candidates) < 4:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
        start = text.find("{", start + 1)
    for c in candidates:
        for attempt in (c, re.sub(r",\s*([}\]])", r"\1", c)):
            try:
                v = json.loads(attempt)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                pass
    raise LLMError("no valid JSON in response: " + text[:120].replace("\n", " "))


# ---------------------------------------------------------------- provider calls
def _client(provider: str, key: str, timeout: float) -> AsyncOpenAI:
    k = (provider, key)
    if k not in _clients:
        _clients[k] = AsyncOpenAI(api_key=key, base_url=BASE_URLS[provider], max_retries=0, timeout=timeout)
    return _clients[k]


async def _call_openai(m: Model, key: str, system: str, user: str, image: Optional[bytes], max_tokens: int) -> str:
    content = user
    if image:
        content = [{"type": "text", "text": user},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}}]
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    if m.provider == "openai":   # current OpenAI models: reasoning models, no temperature, max_completion_tokens
        kwargs = dict(model=m.name, max_completion_tokens=max_tokens, messages=msgs)
    else:
        kwargs = dict(model=m.name, max_tokens=max_tokens, temperature=0.3, messages=msgs)
    if m.json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    kwargs.update(m.extra)
    r = await _client(m.provider, key, m.timeout).chat.completions.create(**kwargs)
    if r.usage:                  # paid models: keep exact token counts so the report can show the real cost
        u = USAGE.setdefault(f"{m.provider}/{m.name}", {"in": 0, "cached": 0, "out": 0})
        u["in"] += r.usage.prompt_tokens or 0
        u["out"] += r.usage.completion_tokens or 0
        u["cached"] += getattr(getattr(r.usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    return r.choices[0].message.content or ""


async def _call_gemini_native(m: Model, key: str, system: str, user: str, image: Optional[bytes], max_tokens: int) -> str:
    # Gemma rejects system_instruction, so the system text is folded into the prompt.
    parts = [{"text": system + "\n\n" + user}]
    if image:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m.name}:generateContent"
    # Gemma 4 burns the whole token budget on hidden "thinking" unless the level is minimal (3s instead of 20-60s)
    gen = {"maxOutputTokens": max_tokens, "temperature": 0.3, "thinkingConfig": {"thinkingLevel": "minimal"}}
    r = await _http.post(url, headers={"x-goog-api-key": key}, timeout=m.timeout,
                         json={"contents": [{"parts": parts}], "generationConfig": gen})
    if r.status_code != 200:
        raise _HTTPFail(r.status_code, r.text[:160])
    cands = r.json().get("candidates") or []
    if not cands:
        raise LLMError("gemini returned no candidates")
    ps = cands[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in ps if not p.get("thought"))


class _HTTPFail(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


def _status_of(e: Exception) -> Optional[int]:
    return getattr(e, "status", None) or getattr(e, "status_code", None)


# ---------------------------------------------------------------- public API
class LLM:
    async def json(self, role: str, system: str, user: str, image: Optional[bytes] = None,
                   max_tokens: int = 1200, validate=None) -> dict:
        """Run the role's fallback chain until one model returns valid JSON. `validate(dict)` may raise to reject."""
        chain = CHAINS[role]
        errors: list[str] = []
        for idx, m in enumerate(chain):
            mk = f"{m.provider}/{m.name}"
            last = idx == len(chain) - 1
            if _bad_until.get(mk, 0) > time.time() and not last:
                continue
            pool = POOLS[m.provider]
            if m.provider in PAID and not paid_available(role):
                continue                           # no key, or this run's paid-call budget is used up
            for _ in range(m.tries):
                if m.provider in PAID:
                    if not paid_available(role):
                        break
                    BUDGET["calls_left"] -= 1
                ki, key = pool.next()
                t0 = time.time()
                try:
                    if m.provider == "gemini":
                        text = await _call_gemini_native(m, key, system, user, image, max_tokens)
                    else:
                        text = await _call_openai(m, key, system, user, image, max_tokens)
                    data = extract_json(text)
                    if validate:
                        validate(data)
                    _stat(m, True, time.time() - t0)
                    _streak[mk] = 0
                    return data
                except Exception as e:  # noqa: BLE001 - every failure mode just means "try the next key/model"
                    _stat(m, False, time.time() - t0, str(_status_of(e) or type(e).__name__)[:30])
                    _streak[mk] = _streak.get(mk, 0) + 1
                    if _streak[mk] >= 4:
                        _bad_until[mk] = time.time() + 45
                    status = _status_of(e)
                    errors.append(f"{mk}: {type(e).__name__} {status or ''} {str(e)[:90]}")
                    st = STATS[mk]
                    st["samples"] = (st.get("samples", []) + [f"{time.time() - t0:.0f}s {str(e)[:160]}"])[-3:]
                    ERRORS.append(errors[-1])
                    del ERRORS[:-40]
                    if status == 429:
                        pool.cool(ki, 20)
                    elif status in (500, 502, 503, 504):
                        pool.cool(ki, 3)
                    elif status in (400, 404, 422) and "json" not in str(e).lower():
                        break                      # (Groq's json_validate_failed is a 400 worth retrying)                      # request itself is unsupported by this model: next model
                    await asyncio.sleep(0.25)
        raise LLMError(f"all models failed for role '{role}': " + " | ".join(errors[-4:]))


llm = LLM()
