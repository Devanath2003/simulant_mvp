"""FastAPI server: REST to start/list runs, WebSocket to stream a run, static UI."""
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())   # Playwright needs subprocess support

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from playwright.async_api import async_playwright

from backend.config import RUNS_DIR, ROOT
from backend.report.pdf import write_pdf
from backend.runner import Run, execute_run, load_personas

RUNS_DIR.mkdir(exist_ok=True)
app = FastAPI(title="Simulant")
RUNS: dict[str, Run] = {}
app.mount("/runs-files", StaticFiles(directory=RUNS_DIR), name="runs-files")


class StartReq(BaseModel):
    url: str
    personas: list[str]
    goal: str = ""
    authorized: bool = False     # tester confirms they own / may test the site (required for the integrity persona)


app.mount("/assets", StaticFiles(directory=ROOT / "frontend" / "assets"), name="assets")


@app.get("/")
def landing():
    return FileResponse(ROOT / "frontend" / "landing.html")


@app.get("/app")
def dashboard():
    return FileResponse(ROOT / "frontend" / "app.html")


@app.get("/api/personas")
def personas():
    return [{"id": p.id, "name": p.name, "engine": p.engine, "description": p.description, "device": p.device,
             "network": p.network, "patience_s": p.patience_s, "abandon_at": p.abandon_at, "zoom": p.zoom,
             "rage_click": p.rage_click, "max_steps": p.max_steps} for p in load_personas().values()]


@app.post("/api/runs")
async def start(req: StartReq):
    u = urlparse(req.url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPException(400, "url must start with http:// or https://")
    known = load_personas()
    bad = [p for p in req.personas if p not in known]
    if bad or not req.personas:
        raise HTTPException(400, f"unknown or empty persona list: {bad}")
    if any(known[p].engine == "integrity" for p in req.personas) and not req.authorized:
        raise HTTPException(400, "The Integrity Engine tries to abuse business logic: confirm you own or are authorised to test this site.")
    try:                                     # fail fast with a clear message instead of 3 personas crashing
        async with httpx.AsyncClient(follow_redirects=True, verify=False, timeout=10) as c:
            await c.get(req.url)
    except httpx.HTTPError as e:
        hint = " (for the demo shop run: python serve.py --demo)" if u.hostname in ("127.0.0.1", "localhost") and u.port == 8001 else ""
        raise HTTPException(400, f"Cannot reach {req.url}: {type(e).__name__}. Is the site running?{hint}")
    run = Run(req.url, req.personas, req.goal, console=True)
    print(f"\n=== run {run.id} started: {req.url} ({', '.join(req.personas)}) ===", flush=True)
    RUNS[run.id] = run
    asyncio.create_task(execute_run(run))
    return {"run_id": run.id}


def _disk_events(run_id: str) -> list[dict]:
    f = RUNS_DIR / run_id / "events.jsonl"
    if not f.exists():
        raise HTTPException(404, "run not found")
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_summary(d) -> dict | None:
    """One row of the dashboard's run history, built from the files every run leaves behind."""
    ev_file = d / "events.jsonl"
    if not (d.is_dir() and ev_file.exists()):
        return None
    with ev_file.open(encoding="utf-8") as fh:
        first = json.loads(fh.readline() or "{}")
    info = {"run_id": d.name, "url": first.get("url", ""), "goal": first.get("goal", ""),
            "personas": [p["id"] for p in first.get("personas", [])], "status": "failed", "score": None}
    if d.name in RUNS and RUNS[d.name].status == "running":
        info["status"] = "running"
    rep = d / "report.json"
    if rep.exists():
        r = json.loads(rep.read_text(encoding="utf-8"))
        sev = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for f in r["findings"]:
            sev[f["severity"]] += 1
        info.update(status="done", score=r["score"]["overall"], vitals=r["score"]["vitals"], findings=len(r["findings"]),
                    severity=sev, elapsed_s=r.get("elapsed_s"), advice=r.get("release_advice", ""),
                    outcomes={j["persona"]: j["status"] for j in r["journeys"]})
    return info


@app.get("/api/runs")
def list_runs():
    rows = [_run_summary(d) for d in sorted(RUNS_DIR.iterdir(), reverse=True)[:60]]
    return [r for r in rows if r]


@app.get("/api/runs/{run_id}/report")
def run_report(run_id: str):
    if not re.fullmatch(r"[\w-]+", run_id):
        raise HTTPException(400, "bad run id")
    rep = RUNS_DIR / run_id / "report.json"
    if not rep.exists():
        raise HTTPException(404, "this run has no report yet")
    return json.loads(rep.read_text(encoding="utf-8"))


@app.get("/api/runs/{run_id}/report.pdf")
async def report_pdf(run_id: str):
    if not re.fullmatch(r"[\w-]+", run_id):
        raise HTTPException(400, "bad run id")
    run_dir = RUNS_DIR / run_id
    pdf, rep = run_dir / "report.pdf", run_dir / "report.json"
    if not pdf.exists():                     # older run, or export failed: build it now
        if not rep.exists():
            raise HTTPException(404, "this run has no report yet")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                await write_pdf(browser, json.loads(rep.read_text(encoding="utf-8")), run_dir)
            finally:
                await browser.close()
    return FileResponse(pdf, media_type="application/pdf", filename=f"Simulant_report_{run_id}.pdf")


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str):
    return RUNS[run_id].events if run_id in RUNS else _disk_events(run_id)


@app.websocket("/ws/{run_id}")
async def stream(ws: WebSocket, run_id: str):
    await ws.accept()
    try:
        run = RUNS.get(run_id)
        if not run:                                  # finished run on disk: replay it
            for ev in _disk_events(run_id):
                await ws.send_json(ev)
            await ws.close()
            return
        q = run.subscribe()                          # subscribe + snapshot with no await between: nothing lost, nothing doubled
        snapshot = list(run.events)
        finished = False
        for ev in snapshot:
            await ws.send_json(ev)
            finished = finished or ev["type"] == "run_end"
        while not finished:
            ev = await q.get()
            await ws.send_json(ev)
            finished = ev["type"] == "run_end"
        run.subs.discard(q)
        await ws.close()
    except (WebSocketDisconnect, HTTPException, RuntimeError):
        pass
