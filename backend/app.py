"""FastAPI server: REST to start/list runs, WebSocket to stream a run, static UI."""
import asyncio
import json
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

from backend.config import RUNS_DIR, ROOT
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


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend" / "index.html")


@app.get("/api/personas")
def personas():
    return [{"id": p.id, "name": p.name, "engine": p.engine, "description": p.description} for p in load_personas().values()]


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


@app.get("/api/runs")
def list_runs():
    out = []
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        rep = d / "report.json"
        if d.is_dir() and (d / "events.jsonl").exists():
            info = {"run_id": d.name, "done": rep.exists()}
            if rep.exists():
                r = json.loads(rep.read_text(encoding="utf-8"))
                info.update(url=r["url"], score=r["score"]["overall"], findings=len(r["findings"]))
            out.append(info)
    return out[:30]


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
