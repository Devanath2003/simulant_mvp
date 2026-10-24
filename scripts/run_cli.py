"""Run Simulant from the terminal (no UI):  python scripts/run_cli.py <url> <persona,persona,...> [goal]"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from backend.runner import Run, execute_run  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp1252

SEV = {"critical": "CRIT", "high": "HIGH", "medium": "MED ", "low": "LOW "}


async def main():
    url = sys.argv[1]
    personas = (sys.argv[2] if len(sys.argv) > 2 else "senior,mobile_3g,deal_hunter").split(",")
    goal = sys.argv[3] if len(sys.argv) > 3 else ""
    run = Run(url, personas, goal)
    q = run.subscribe()
    task = asyncio.create_task(execute_run(run))
    while True:
        ev = await q.get()
        t, p = ev["type"], ev.get("persona", "")
        if t == "thought":
            print(f"[{ev['t']:6.1f}s] {p:11s} #{ev['step']:<2} F={ev['frustration']:<3} {ev['action']:<42} | {ev['thought']}")
        elif t == "action":
            print(f"{'':9s}   {p:11s}    -> {ev['result']}  {('reqs=' + str(ev['requests'])) if ev['requests'] else ''}")
        elif t == "finding":
            f = ev["finding"]
            print(f"{'':9s}   {p:11s}    !! [{SEV[f['severity']]}] {f['vital']}: {f['title'][:110]}")
        elif t in ("persona_start", "persona_end", "log", "error", "run_start"):
            print(f"[{ev['t']:6.1f}s] {t.upper()} {p} " + str({k: v for k, v in ev.items() if k not in ('t', 'type', 'persona', 'personas')})[:300])
        elif t == "report":
            r = ev["report"]
            print("\n==== REPORT ====")
            print("score:", r["score"], "| elapsed", r["elapsed_s"], "s |", r["release_advice"])
            print(r["summary"])
            for f in r["findings"]:
                print(f"  {f['id']:4s}[{SEV[f['severity']]}] {f['vital']:13s} {f['title'][:100]}  <- {','.join(f['personas'])}")
            print("llm usage:", r["llm_usage"])
            print("paid usage:", r.get("paid_usage"))
        elif t == "run_end":
            break
    await task
    print("run dir:", run.dir)


asyncio.run(main())
