"""Run manager: launches one browser, runs the chosen personas concurrently, streams events, builds the report."""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

from backend.config import RUNS_DIR
from backend.llm.gateway import LLMError, llm
from backend.models import Finding, Persona
from backend.navigator import prompts
from backend.navigator.agent import Ctx, build_graph
from backend.navigator.browser import Session
from backend.report.aggregate import build_report

PERSONA_DIR = Path(__file__).parent / "personas"
PERSONA_TIMEOUT_S = 600


def load_personas() -> dict[str, Persona]:
    out = {}
    for f in sorted(PERSONA_DIR.glob("*.yaml")):
        p = Persona(**yaml.safe_load(f.read_text(encoding="utf-8")))
        out[p.id] = p
    return out


SEV_TAG = {"critical": "CRIT", "high": "HIGH", "medium": "MED ", "low": "LOW "}


def console_line(ev: dict) -> str | None:
    """One readable terminal line per interesting event (None = don't print)."""
    t, p = ev["type"], ev.get("persona", "")
    head = f"[{ev['t']:6.1f}s] {p:11s}"
    if t == "run_start":
        return f"[{ev['t']:6.1f}s] RUN START  {ev['url']}  personas: {', '.join(x['id'] for x in ev['personas'])}"
    if t == "persona_start":
        return f"{head} goal: {ev['goal'][:140]}"
    if t == "thought":
        return f"{head} #{ev['step']:<2} F={ev['frustration']:<3} {ev['action'][:45]:<45} | {ev['thought'][:110]}"
    if t == "action":
        return f"{'':22s}   -> {ev['result'][:120]}"
    if t == "finding":
        f = ev["finding"]
        return f"{'':22s}   !! [{SEV_TAG[f['severity']]}] {f['vital']}: {f['title'][:100]}"
    if t == "persona_end":
        return f"{head} END {ev['status'].upper()}: {ev.get('reason', '')[:150]}"
    if t == "log":
        return f"{head} (i) {ev['msg'][:150]}"
    if t == "report":
        r = ev["report"]
        return f"[{ev['t']:6.1f}s] REPORT  score {r['score']['overall']}/100, {len(r['findings'])} findings - {r['release_advice'][:80]}"
    if t == "error":
        return f"[{ev['t']:6.1f}s] RUN FAILED: {ev['message'][:200]}"
    if t == "run_end":
        return f"[{ev['t']:6.1f}s] RUN END ({ev['status']})"
    return None


class Run:
    def __init__(self, url: str, persona_ids: list[str], goal: str = "", console: bool = False):
        self.console = console            # print events to the terminal (the server does, the CLI prints its own)
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.url, self.goal, self.persona_ids = url, goal.strip(), persona_ids
        self.dir = RUNS_DIR / self.id
        (self.dir / "shots").mkdir(parents=True, exist_ok=True)
        self.events: list[dict] = []
        self.subs: set[asyncio.Queue] = set()
        self.status = "running"
        self.report: dict | None = None
        self.t0 = time.time()
        self._log = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def emit(self, type: str, **data):
        ev = {"t": round(time.time() - self.t0, 2), "type": type, **data}
        self.events.append(ev)
        self._log.write(json.dumps(ev, default=str) + "\n")
        self._log.flush()
        if self.console and (line := console_line(ev)):
            print(line, flush=True)
        for q in list(self.subs):
            q.put_nowait(ev)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subs.add(q)
        return q


async def run_persona(run: Run, pw, browser, persona: Persona) -> dict:
    sess = Session(browser, pw, persona, run.url)
    result = {"persona": persona.id, "status": "error", "reason": "", "steps": 0, "goal": "", "findings": []}
    try:
        await sess.start()
        obs = await sess.observe()
        goal, focus = run.goal, []
        try:
            def _ok(d):
                if len(str(d.get("goal", "")).strip()) < 20:
                    raise ValueError("goal missing or echoed the template")
                if persona.engine == "empathy" and len(d.get("focus") or []) < 4:
                    raise ValueError("an empathy goal needs at least 4 ordered steps (a full flow, not just browsing)")
            plan = await llm.json("planner", prompts.PLANNER_SYSTEM, prompts.planner_user(persona, obs, run.goal),
                                  max_tokens=900, validate=_ok)
            goal = str(plan.get("goal") or "").strip()
            focus = [str(x) for x in (plan.get("focus") or [])][:5]
        except LLMError as e:
            run.emit("log", persona=persona.id, msg="planner failed, using default goal: " + str(e)[:100])
        if not goal:
            goal = run.goal or ("Browse the site like a normal visitor and complete its main task (e.g. buy something)."
                                if persona.engine == "empathy" else
                                "Buy a product normally, then look for ways to pay less than you should.")
        run.emit("persona_start", persona=persona.id, name=persona.name, engine=persona.engine, goal=goal, focus=focus)
        ctx = Ctx(run_id=run.id, run_dir=run.dir, persona=persona, session=sess, goal=goal, focus=focus,
                  emit=run.emit, site_url=run.url)
        graph = build_graph(ctx)
        final = await asyncio.wait_for(
            graph.ainvoke({"step": 0, "frustration": 0, "status": "running", "history": [], "recent": [], "why": []},
                          {"recursion_limit": 4 * persona.max_steps + 40}),
            timeout=PERSONA_TIMEOUT_S)
        result.update(status=final.get("status", "failure"), reason=final.get("reason", ""), steps=final.get("step", 0),
                      goal=goal, findings=ctx.findings, frustration=final.get("frustration", 0))
    except asyncio.TimeoutError:
        result.update(status="error", reason=f"timed out after {PERSONA_TIMEOUT_S}s")
        run.emit("persona_end", persona=persona.id, status="error", reason=result["reason"])
    except Exception as e:  # noqa: BLE001 - one persona crashing must not kill the run
        result.update(status="error", reason=f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}")
        run.emit("persona_end", persona=persona.id, status="error", reason=result["reason"])
    finally:
        await sess.close()
    return result


async def execute_run(run: Run):
    personas = load_personas()
    chosen = [personas[i] for i in run.persona_ids if i in personas]
    run.emit("run_start", url=run.url, personas=[{"id": p.id, "name": p.name, "engine": p.engine} for p in chosen], goal=run.goal)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=os.getenv("HEADED") != "1")
            try:
                results = await asyncio.gather(*[run_persona(run, pw, browser, p) for p in chosen])
            finally:
                await browser.close()
        if all(r["status"] == "error" for r in results):    # nothing was tested: a report would only show fake 100s
            raise RuntimeError("no persona could test the site - " + results[0]["reason"])
        run.report = await build_report(run, results)
        (run.dir / "report.json").write_text(json.dumps(run.report, indent=2, default=str), encoding="utf-8")
        run.emit("report", report=run.report)
        run.status = "done"
    except Exception as e:  # noqa: BLE001
        run.status = "error"
        run.emit("error", message=str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}")
    finally:
        run.emit("run_end", status=run.status)
