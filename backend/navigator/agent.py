"""One persona = one LangGraph run:  observe -> decide -> execute -> observe ... -> finalize."""
import ast
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TypedDict
from urllib.parse import urlparse

from langgraph.graph import END, StateGraph

from backend.llm.gateway import LLMError, llm, paid_available
from backend.models import ActorOutput, Finding, Persona
from backend.navigator import prompts, sensors
from backend.navigator.browser import ActionResult, Session

ERROR_WORDS = re.compile(r"error|invalid|failed|incorrect|required|not valid|try again|denied|unable|wrong", re.I)
NEG_TOTAL = re.compile(r"(total|amount|payable|grand|you pay|to pay|balance|charged)[^\n\d-]{0,25}[-−]\s*[₹$€£]?\s*\d", re.I)
SEV_FROM_CONF = lambda c: "high" if c >= 7 else "medium" if c >= 4 else "low"  # noqa: E731


class S(TypedDict, total=False):
    step: int
    obs: dict
    prev_obs: dict
    frustration: int
    memory: str
    history: list
    last_result: str
    last_desc: str
    last_thought: str
    last_requests: list
    action: dict
    status: str            # running | success | failure | abandoned | error
    reason: str
    llm_fail: int
    prev_alerts: list
    hint: str
    why: list              # reasons frustration went up
    recent: list           # (action, target, value, changed) for the loop guard
    looked: bool
    tried: dict            # integrity: abuse idea -> number of actions spent on it
    done_refused: int
    nudge: str
    agent_error: bool


@dataclass
class Ctx:
    run_id: str
    run_dir: Path
    persona: Persona
    session: Session
    goal: str
    focus: list
    emit: Callable
    site_url: str
    findings: list = field(default_factory=list)
    bg: list = field(default_factory=list)
    seen_pages: set = field(default_factory=set)
    identity: dict = field(default_factory=dict)
    seen_audit: set = field(default_factory=set)
    visited: list = field(default_factory=list)
    shot: str = ""         # latest screenshot (path relative to run dir)
    shot_bytes: bytes = b""
    quote: str = ""
    stats: dict = field(default_factory=lambda: {"steps": 0, "llm_calls": 0})

    def add(self, f: Finding):
        if not f.persona_quote:
            f.persona_quote = self.quote
        self.findings.append(f)
        self.emit("finding", persona=self.persona.id, finding=f.model_dump())


# --------------------------------------------------------------------------- helpers
def _as_field_list(v) -> list:
    """Models sometimes put the fill list in "value", or as a JSON/Python string - accept all of those."""
    if isinstance(v, str) and v.strip().startswith("["):
        for parse in (json.loads, ast.literal_eval):
            try:
                v = parse(v)
                break
            except (ValueError, SyntaxError):
                pass
    if isinstance(v, dict):
        v = [{"target": k, "value": val} for k, val in v.items()]
    return v if isinstance(v, list) and all(isinstance(x, dict) for x in v) else []


def resolve_target(t, obs: dict | None):
    """Element id from whatever the model wrote: 3, "3", "[3]", or the element's label ("Username")."""
    if t is None or t == "":
        return None
    if isinstance(t, (int, float)):
        return int(t)
    s = str(t).strip()
    m = re.fullmatch(r"\[?\s*(\d+)\s*\]?", s)
    if m:
        return int(m.group(1))
    if obs:
        want = s.lower().strip('"\' ')
        items = [(e["id"], (e.get("name") or "").lower()) for e in obs.get("elements", [])]
        items += [(h["id"], h["name"].lower()) for h in obs.get("hidden", [])]
        for match in (lambda n: n == want, lambda n: n.rstrip(" *") == want.rstrip(" *"), lambda n: want in n or (n and n in want)):
            hits = [i for i, n in items if match(n)]
            if hits:
                return hits[0]
    return None


def clean_actor(data: dict, integrity: bool, obs: dict | None = None) -> ActorOutput:
    data = dict(data)
    if not data.get("fields") and _as_field_list(data.get("value")):
        data["fields"], data["value"] = _as_field_list(data["value"]), None
        data["action"] = "fill"
    data["fields"] = _as_field_list(data.get("fields"))
    if data["fields"] and data.get("action") in ("type", "fill", "select", None):
        data["action"] = "fill"
    d = {k: data.get(k) for k in ActorOutput.model_fields if k in data}
    d["target"] = resolve_target(d.get("target"), obs)
    try:
        d["confusion"] = int(float(d.get("confusion") or 0))
    except (TypeError, ValueError):
        d["confusion"] = 0
    d["confusion"] = max(0, min(10, d.get("confusion") or 0))
    for k in ("thought", "ux_note", "value"):
        if d.get(k) is not None and not isinstance(d[k], str):
            d[k] = str(d[k])
    if isinstance(d.get("success"), str):
        d["success"] = d["success"].lower() == "true"
    fields = []
    for f in d.get("fields") or []:
        tid = resolve_target(f.get("target", f.get("id", f.get("field"))), obs)
        if tid is not None:
            fields.append({"target": tid, "value": "" if f.get("value") is None else str(f["value"])})
    d["fields"] = fields
    if d.get("action") == "fill" and not fields:          # "fill" with a single target/value is just a type
        d["action"] = "type"
    a = ActorOutput(**d)
    if not integrity and a.action in ("tamper", "repeat", "goto", "fill_form"):
        a = ActorOutput(thought=a.thought, action="wait", value="1", ux_note="", confusion=a.confusion)
    return a


def names_by_id(obs: dict) -> dict:
    n = {e["id"]: (e["name"] or ("icon-only button" if e.get("icon_only") else "unlabeled element")) for e in obs.get("elements", [])}
    n.update({h["id"]: f'hidden field "{h["name"]}"' for h in obs.get("hidden", [])})
    return n


def describe(a: ActorOutput, obs: dict) -> str:
    if a.action == "fill":
        names = names_by_id(obs)
        parts = [f"[{f['target']}] {names.get(f['target'], '?')[:18]}={f['value'][:18]!r}" for f in a.fields[:6]]
        return f"fill {len(a.fields)} fields: " + ", ".join(parts) + (" ..." if len(a.fields) > 6 else "")
    nm = names_by_id(obs).get(a.target, "") if a.target is not None else ""
    t = f' [{a.target}] "{nm}"' if a.target is not None else ""
    v = f" = {a.value!r}" if a.value not in (None, "") and a.action in ("type", "select", "tamper", "goto", "repeat") else ""
    return f"{a.action}{t}{v}"


EYES_PER_PERSONA = 12


def stuck_reason(s: dict) -> str:
    """Why the last step suggests navigation went wrong (the cheap text-only actor is likely misreading the page)."""
    recent, last = s.get("recent", []), s.get("last_result", "")
    if s.get("agent_error"):
        return "your last action could not be carried out (" + last.replace("FAILED: ", "")[:90] + ")"
    if "covering" in last:
        return "your last click was blocked because something on screen is covering that element"
    if recent and recent[-1][0] in ("click", "repeat") and not recent[-1][3]:
        return "your last click had no visible effect"
    if len(recent) >= 3 and all(r[0] == "scroll" for r in recent[-3:]):
        return "you scrolled 3 times in a row without finding what you need"
    return ""


def path_of(url: str) -> str:
    u = urlparse(url)
    return u.path or "/"


# --------------------------------------------------------------------------- graph
def build_graph(ctx: Ctx):
    P, sess = ctx.persona, ctx.session
    integrity = P.engine == "integrity"
    actor_role = "actor_integrity" if integrity else "actor"

    # ---------------------------------------------------------------- observe
    async def observe(s: S) -> dict:
        step = s.get("step", 0) + 1
        if step > P.max_steps:
            return {"status": "failure", "reason": f"ran out of patience/steps ({P.max_steps}) before finishing the goal"}
        try:
            obs = await sess.observe()
        except Exception:  # noqa: BLE001 - page was mid-navigation or busy: give it a moment, try once more
            await asyncio.sleep(1.5)
            obs = await sess.observe()
        jpg = await sess.screenshot()
        rel = f"shots/{P.id}_{step:02d}.jpg" if jpg else ""
        if jpg:
            (ctx.run_dir / rel).write_bytes(jpg)
        ctx.shot, ctx.shot_bytes = rel, jpg
        ctx.emit("screen", persona=P.id, step=step, url=obs["url"], title=obs["title"], shot=rel,
                 n_elements=len(obs["elements"]))
        upd: dict = {"step": step, "obs": obs, "prev_obs": s.get("obs", {}), "hint": s.get("hint", "")}
        ctx.stats["steps"] = step

        key = path_of(obs["url"])
        if key not in ctx.seen_pages:                       # first time on this page: sensors + perceiver
            ctx.seen_pages.add(key)
            ctx.visited.append(key)
            if not integrity:                                # UX/accessibility belongs to the Empathy Engine
                for f in sensors.audit_findings(P, obs["url"], await sess.audit(), step, rel):
                    kind = f.title.rsplit(" (", 1)[0]
                    if kind not in ctx.seen_audit:           # site-wide issues (header icon, contrast...) are reported once
                        ctx.seen_audit.add(kind)
                        ctx.add(f)
                for f in sensors.load_time_finding(P, obs["url"], sess.load_ms.get(obs["url"], 0), step, rel):
                    ctx.add(f)
                if jpg:
                    ctx.bg.append(asyncio.create_task(perceive(ctx, obs, jpg, step, rel)))

        fr, why = s.get("frustration", 0), list(s.get("why", []))
        new_alerts = [a for a in obs["alerts"] if a not in s.get("prev_alerts", [])]
        upd["prev_alerts"] = obs["alerts"]
        if new_alerts and any(ERROR_WORDS.search(a) for a in new_alerts):
            fr = min(100, fr + 10)
            why.append(f"error message: {new_alerts[0][:60]}")
        upd["frustration"], upd["why"] = fr, why

        if integrity and s.get("last_desc"):                # did the last abuse attempt break a business rule?
            po = s.get("obs", {})                            # full page text: totals are often outside the viewport
            prev_txt, cur_txt = po.get("full_text", po.get("text", "")), obs.get("full_text", obs["text"])
            if NEG_TOTAL.search(cur_txt) and not NEG_TOTAL.search(prev_txt):
                ctx.add(Finding(persona=P.id, engine="integrity", vital="integrity", severity="critical", source="oracle",
                                category="invalid_quantity", title="Negative amount accepted in the cart/total", url=obs["url"], step=step, screenshot=rel,
                                detail=f"After '{s['last_desc']}' the page shows a negative amount.",
                                suggested_fix="Validate quantity/price server-side and clamp totals at zero."))
            if prev_txt != cur_txt or s.get("last_requests"):
                ctx.bg.append(asyncio.create_task(judge(ctx, dict(s), obs, step, rel)))
        return upd

    # ---------------------------------------------------------------- decide
    async def decide(s: S) -> dict:
        obs = s["obs"]
        warnings = []
        recent = s.get("recent", [])
        if len(recent) >= 3 and len(set(recent[-3:])) == 1 and not recent[-1][3]:
            warnings.append("You already repeated the exact same action 3 times without effect. Try something different, or give_up.")
        if len(recent) >= 5 and len(set(recent[-5:])) == 1:
            if s.get("agent_error"):         # our own malformed actions are not the site's fault: no finding
                ctx.emit("log", persona=P.id, msg="navigator could not operate this page: " + s.get("last_result", "")[:120])
                return {"status": "error", "reason": "navigator problem (not a site issue): " + s.get("last_result", "")[:150]}
            ctx.add(Finding(persona=P.id, engine=P.engine, vital="usability", severity="high", source="sensor",
                            title=f"User stuck repeating '{s.get('last_desc', '')}' with no effect", url=obs["url"],
                            step=s["step"], screenshot=ctx.shot,
                            detail="The persona kept trying the same action and never got any response.",
                            suggested_fix="Give clear feedback (loading state, error text) when an action cannot proceed."))
            return {"status": "failure", "reason": "got stuck repeating the same action with no effect"}

        if s.get("nudge"):
            warnings.append(s["nudge"])
        for k, n in s.get("tried", {}).items():
            if n >= 4:
                warnings.append(f"You already spent {n} actions on '{k}'. Move on to an idea you have NOT attempted yet, or finish with done.")
        system = prompts.actor_system(P, ctx.goal, ctx.focus, ctx.site_url, ctx.identity)
        user = prompts.actor_user(P, s, obs, warnings, ctx.visited, ctx.goal)

        async def eyes(reason: str):
            """Vision navigator: a paid multimodal model looks at the numbered screenshot and takes this one step."""
            jpg = await sess.som_screenshot()
            d = await llm.json("navigator", system, user + prompts.eyes_note(reason), image=jpg, max_tokens=900)
            ctx.stats["eyes"] = ctx.stats.get("eyes", 0) + 1
            ctx.emit("log", persona=P.id, msg=f"vision navigator ({reason[:70]}) -> {d.get('action')} {d.get('target') or ''}")
            return d, clean_actor(d, integrity, obs)

        used_eyes = False
        try:
            # not for the Deal-Hunter: OpenAI models decline to keep attempting abuses (measured)
            can_see = not integrity and paid_available() and ctx.stats.get("eyes", 0) < EYES_PER_PERSONA
            reason = stuck_reason(s) if can_see else ""
            if reason:                                       # navigation went wrong last step: let the eyes take this one
                try:
                    (data, a), used_eyes = await eyes(reason), True
                except Exception as e:  # noqa: BLE001 - optional helper: any failure falls back to the free actor
                    ctx.emit("log", persona=P.id, msg=f"vision navigator unavailable: {str(e)[:100]}")
            if not used_eyes:
                data = await llm.json(actor_role, system, user, max_tokens=600)
                a = clean_actor(data, integrity, obs)
                ctx.stats["llm_calls"] += 1
                second_look = ("you asked to look at the screen: " + (a.value or a.thought)[:80] if a.action == "look" else
                               "you are about to give up - check the screen once more for a way forward you may have missed"
                               if a.action == "give_up" and not integrity else "")
                if second_look and can_see:
                    try:
                        (data, a), used_eyes = await eyes(second_look), True
                    except Exception as e:  # noqa: BLE001
                        ctx.emit("log", persona=P.id, msg=f"vision navigator unavailable: {str(e)[:100]}")
                if a.action == "look":                       # no paid budget: free vision grounder (Set-of-Marks) + one more actor call
                    hint = await ground(ctx, a.value or a.thought, obs)
                    data = await llm.json(actor_role, system, user + f"\n\nVision helper: {hint}\nNow choose your action.", max_tokens=600)
                    a = clean_actor(data, integrity, obs)
                    ctx.stats["llm_calls"] += 2
            if a.action == "look":
                a = ActorOutput(thought=a.thought, action="wait", value="1")
        except (LLMError, ValueError) as e:
            n = s.get("llm_fail", 0) + 1
            ctx.emit("log", persona=P.id, msg=f"LLM failure {n}/3: {str(e)[:140]}")
            if n >= 3:
                return {"status": "error", "reason": "the AI models were unreachable: " + str(e)[:140], "llm_fail": n}
            return {"llm_fail": n, "action": {"action": "wait", "value": "1"}}

        if integrity and a.action == "done":
            todo = [k for k in prompts.ATTACKS if k not in s.get("tried", {})]
            if len(todo) >= 2 and s["step"] < P.max_steps - 4 and s.get("done_refused", 0) < 3:
                ctx.emit("log", persona=P.id, msg=f"finish refused, untried ideas: {', '.join(todo[:5])}")
                return {"action": {"action": "wait", "value": "1"}, "done_refused": s.get("done_refused", 0) + 1,
                        "nudge": "You tried to finish, but these abuse ideas were never SUBMITTED to the server: " + ", ".join(todo)
                                 + ". Do them now (type the value, then click the submit/Update/Apply/Pay button)."}
        if a.action == "give_up" and s.get("frustration", 0) < 25:
            # not frustrated enough to quit: the goal is just not achievable, which is not a UX drop-off
            a = ActorOutput(thought=a.thought, action="done", success=False, ux_note=a.ux_note, confusion=a.confusion)
        ctx.quote = a.thought
        upd: dict = {"nudge": "", "action": a.model_dump(), "llm_fail": 0, "memory": str(data.get("memory") or s.get("memory", ""))[:300]}
        fr, why = s.get("frustration", 0), list(s.get("why", []))
        if a.confusion >= 4:
            fr = min(100, fr + a.confusion)
            why.append(f"confusing screen ({a.ux_note[:60] or a.thought[:60]})")
        upd["frustration"], upd["why"] = fr, why
        ctx.emit("thought", persona=P.id, step=s["step"], thought=a.thought, action=describe(a, obs),
                 confusion=a.confusion, frustration=fr, memory=upd["memory"], eyes=used_eyes)
        if a.ux_note.strip() and len(a.ux_note) > 8 and not integrity:
            ctx.add(Finding(persona=P.id, engine=P.engine, vital="usability", severity=SEV_FROM_CONF(a.confusion),
                            source="llm", title=a.ux_note[:110], url=obs["url"], step=s["step"], screenshot=ctx.shot,
                            detail=a.thought, persona_quote=a.thought))
        if a.action in ("done", "give_up"):
            if a.action == "give_up":
                ctx.add(Finding(persona=P.id, engine=P.engine, vital="usability", severity="high", source="llm",
                                title=f"Drop-off: {P.name.split(' - ')[0]} gave up on {path_of(obs['url'])}",
                                url=obs["url"], step=s["step"], screenshot=ctx.shot, persona_quote=a.thought,
                                detail="Reasons: " + "; ".join(s.get("why", [])[-4:]) if s.get("why") else a.thought,
                                suggested_fix="Remove the friction listed in this persona's journey on this page."))
                upd.update(status="abandoned", reason=a.thought or "gave up")
            else:
                upd.update(status="success" if a.success is not False else "failure", reason=a.thought)
        return upd

    # ---------------------------------------------------------------- execute
    async def execute(s: S) -> dict:
        obs = s["obs"]
        a = ActorOutput(**s["action"])
        desc = describe(a, obs)
        res: ActionResult = await sess.act(a)
        hist = (s.get("history", []) + [f"{desc} -> {res.summary()}"])[-8:]
        recent = (s.get("recent", []) + [(a.action, a.target, a.value, res.changed and res.ok)])[-6:]
        fr, why = s.get("frustration", 0), list(s.get("why", []))
        nm = names_by_id(obs).get(a.target, "") if a.target is not None else ""
        progress = res.ok and res.changed and res.url_changed

        if not res.ok:
            if "covering" in res.error or "disabled" in res.error:   # a site problem; other errors are the agent's own mistakes
                fr += 12; why.append(f"could not {a.action} '{nm}': {res.error}")    # and never frustrate the persona
                ctx.add(Finding(persona=P.id, engine=P.engine, vital="usability", severity="high", source="sensor",
                                title=f"Cannot {a.action} '{nm or 'element'}': {res.error[:80]}", url=obs["url"], step=s["step"],
                                screenshot=ctx.shot, detail=f"The {a.action} on [{a.target}] '{nm}' failed: {res.error}",
                                suggested_fix="Make sure interactive elements are not covered by banners/overlays and explain why a control is disabled."))
        elif res.validation:                 # browser refused the form and said why: feedback, a small annoyance
            fr += 4; why.append(f"form not accepted: '{res.validation[0]['name']}' {res.validation[0]['msg'][:50]}")
        elif a.action in ("click", "repeat") and not res.changed:
            fr += 15
            reqs = ", ".join(res.requests) or "none"
            if res.eventually_changed:      # the result arrived, but only after the user's patience ran out
                why.append(f"'{nm}' gave no feedback for {P.patience_s:.0f}s")
                ctx.add(Finding(persona=P.id, engine=P.engine, vital="usability", severity="high" if res.rage_clicks else "medium",
                                source="sensor", url=obs["url"], step=s["step"], screenshot=ctx.shot,
                                title=f"No feedback for {P.patience_s:.0f}s+ after clicking '{nm}'" + (f" (user clicked {res.rage_clicks + 1}x)" if res.rage_clicks else ""),
                                detail=f"Nothing on screen changed for {P.patience_s:.0f}s (no spinner, message or disabled state); the result only appeared later. Requests: {reqs}.",
                                suggested_fix="Show immediate feedback (spinner, disabled button, toast) the moment an action starts."))
            elif res.requests:              # request sent, page came back identical
                why.append(f"clicking '{nm}' changed nothing visible")
                ctx.add(Finding(persona=P.id, engine=P.engine, vital="usability", severity="low", source="sensor",
                                url=obs["url"], step=s["step"], screenshot=ctx.shot, title=f"Clicking '{nm}' had no visible effect",
                                detail=f"The page reloaded to an identical state. Requests: {reqs}.",
                                suggested_fix="Confirm the result of the action to the user (message, updated values)."))
            else:                           # nothing happened at all: dead control
                why.append(f"'{nm}' did nothing")
                ctx.add(Finding(persona=P.id, engine=P.engine, vital="functional", severity="high" if res.rage_clicks else "medium",
                                source="sensor", url=obs["url"], step=s["step"], screenshot=ctx.shot,
                                title=f"'{nm}' does nothing when clicked" + (f" (user clicked {res.rage_clicks + 1}x)" if res.rage_clicks else ""),
                                detail="No DOM change and no network request after the click.",
                                suggested_fix="Wire up the control or remove/disable it."))
        elif a.action == "click" and res.first_change_s and res.first_change_s > max(1.5, P.patience_s * 0.6):
            fr += 8; why.append(f"'{nm}' took {res.first_change_s:.1f}s to react")
            ctx.add(Finding(persona=P.id, engine=P.engine, vital="performance", severity="medium", source="sensor",
                            title=f"Slow response ({res.first_change_s:.1f}s) after clicking '{nm}'", url=obs["url"],
                            step=s["step"], screenshot=ctx.shot,
                            detail=f"First visible reaction took {res.first_change_s:.1f}s; settled after the network went quiet.",
                            suggested_fix="Show a loading state right away and speed up the endpoint."))
        if res.rage_clicks or res.duplicate_posts >= 2:
            if res.duplicate_posts >= 2:
                ctx.add(Finding(persona=P.id, engine=P.engine, vital="integrity" if integrity else "functional", severity="high",
                                source="sensor", category="duplicate_order" if integrity else "", url=obs["url"], step=s["step"], screenshot=ctx.shot,
                                title=f"Repeat clicks on '{nm}' sent {res.duplicate_posts} identical POST requests (risk of duplicate submissions)",
                                detail="Requests: " + ", ".join(res.requests),
                                suggested_fix="Disable the button while submitting and make the endpoint idempotent (idempotency key)."))
        if progress:
            fr -= 8
        fr = max(0, min(100, fr))

        tried = dict(s.get("tried", {}))
        if integrity and a.attack in prompts.ATTACKS and (a.action == "goto" or (a.action in ("click", "repeat") and any(r.startswith("POST") for r in res.requests))):
            tried[a.attack] = tried.get(a.attack, 0) + 1      # only counts once it was actually submitted to the server
        upd: dict = {"tried": tried, "history": hist, "recent": recent, "frustration": fr, "why": why[-8:], "last_desc": desc,
                     "last_result": res.summary(), "last_thought": a.thought, "last_requests": res.requests,
                     # failed for a reason that is the navigator's fault, not the site's (bad id, malformed action...)
                     "agent_error": (not res.ok) and not ("covering" in res.error or "disabled" in res.error)}
        ctx.emit("action", persona=P.id, step=s["step"], desc=desc, result=res.summary(), ok=res.ok, changed=res.changed,
                 first_change_s=res.first_change_s, rage_clicks=res.rage_clicks, requests=res.requests, frustration=fr,
                 agent_error=upd["agent_error"])
        if fr >= P.abandon_at:
            ctx.add(Finding(persona=P.id, engine=P.engine, vital="usability", severity="critical", source="sensor",
                            title=f"Drop-off: {P.name.split(' - ')[0]} abandoned the site (frustration {fr}/100) on {path_of(obs['url'])}",
                            url=obs["url"], step=s["step"], screenshot=ctx.shot, persona_quote=a.thought,
                            detail="What frustrated them: " + "; ".join(why[-5:]),
                            suggested_fix="Fix the friction points listed in this finding; they pushed a realistic user to quit."))
            upd.update(status="abandoned", reason=f"frustration reached {fr}/100: " + "; ".join(why[-3:]))
        return upd

    # ---------------------------------------------------------------- finalize
    async def finalize(s: S) -> dict:
        if ctx.bg:
            await asyncio.wait(ctx.bg, timeout=30)
            for t in ctx.bg:
                if not t.done():
                    t.cancel()
        for f in sensors.runtime_findings(P, sess.console_errors, sess.http_errors, sess.origin):
            ctx.add(f)
        status = s.get("status", "failure")
        ctx.emit("persona_end", persona=P.id, status=status, reason=s.get("reason", ""), steps=s.get("step", 0),
                 frustration=s.get("frustration", 0))
        return {"status": status}

    g = StateGraph(S)
    g.add_node("observe", observe)
    g.add_node("decide", decide)
    g.add_node("execute", execute)
    g.add_node("finalize", finalize)
    g.set_entry_point("observe")
    cont = lambda nxt: (lambda s: "finalize" if s.get("status", "running") != "running" else nxt)  # noqa: E731
    g.add_conditional_edges("observe", cont("decide"), {"decide": "decide", "finalize": "finalize"})
    g.add_conditional_edges("decide", cont("execute"), {"execute": "execute", "finalize": "finalize"})
    g.add_conditional_edges("execute", cont("observe"), {"observe": "observe", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


# --------------------------------------------------------------------------- background helpers
async def perceive(ctx: Ctx, obs: dict, jpg: bytes, step: int, rel: str):
    """Vision model reviews the screenshot as this persona (runs in the background, never blocks navigation)."""
    P = ctx.persona
    try:
        data = await llm.json("vision", prompts.PERCEIVER_SYSTEM, prompts.perceiver_user(P, obs, ctx.goal), image=jpg, max_tokens=700)
    except LLMError as e:
        ctx.emit("log", persona=P.id, msg="perceiver skipped: " + str(e)[:100])
        return
    ctx.emit("perception", persona=P.id, step=step, impression=str(data.get("first_impression", ""))[:200])
    for it in (data.get("issues") or [])[:3]:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        sev = it.get("severity") if it.get("severity") in ("high", "medium", "low") else "medium"
        vital = it.get("vital") if it.get("vital") in ("usability", "accessibility") else "usability"
        ctx.add(Finding(persona=P.id, engine=P.engine, vital=vital, severity=sev, source="llm", url=obs["url"], step=step,
                        screenshot=rel, title=str(it["title"])[:110], detail=str(it.get("detail", ""))[:300],
                        persona_quote=str(data.get("first_impression", ""))[:160]))


async def ground(ctx: Ctx, want: str, obs: dict) -> str:
    """Set-of-Marks: numbered boxes drawn on the screenshot, the vision model picks the matching number."""
    try:
        jpg = await ctx.session.som_screenshot()
        data = await llm.json("grounder", prompts.GROUNDER_SYSTEM, f"Which numbered box is: {want}?", image=jpg, max_tokens=300)
        t = data.get("target")
        ctx.emit("log", persona=ctx.persona.id, msg=f"grounder: '{want[:50]}' -> {t}")
        return f"the element matching '{want}' is [{t}] ({data.get('reason', '')})" if t is not None else f"no element matching '{want}' is visible; try scrolling or another approach"
    except Exception as e:  # noqa: BLE001
        return f"vision helper unavailable ({str(e)[:60]})"


async def judge(ctx: Ctx, s: S, obs: dict, step: int, rel: str):
    """Integrity oracle: did the app enforce its business rule after the last abuse attempt?"""
    P = ctx.persona
    try:
        data = await llm.json("planner", prompts.JUDGE_SYSTEM,
                              prompts.judge_user(s.get("last_desc", ""), s.get("last_thought", ""), s.get("obs", {}), obs, s.get("last_requests", [])),
                              max_tokens=500)
    except LLMError:
        return
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0
    if data.get("violation") is True and conf >= 0.7:
        sev = data.get("severity") if data.get("severity") in ("critical", "high", "medium") else "high"
        cat = str(data.get("category", "other")).strip().lower()
        ctx.add(Finding(persona=P.id, engine="integrity", vital="integrity", severity=sev, source="oracle", url=obs["url"],
                        category=cat, step=step, screenshot=rel, title=str(data.get("title", "Business rule not enforced"))[:120],
                        detail=f"Action: {s.get('last_desc')}. Rule: {data.get('rule', '')}. Evidence: {data.get('evidence', '')}",
                        suggested_fix=str(data.get("fix", ""))[:200], persona_quote=s.get("last_thought", "")))
