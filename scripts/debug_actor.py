"""Debug helper: print the raw actor JSON for the first step on a site.  python scripts/debug_actor.py <url> <persona> "<goal>" """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
from playwright.async_api import async_playwright
from backend.llm.gateway import llm
from backend.navigator import prompts
from backend.navigator.agent import clean_actor
from backend.navigator.browser import Session
from backend.runner import load_personas, test_identity

async def main():
    url, pid, goal = sys.argv[1], sys.argv[2], sys.argv[3]
    P = load_personas()[pid]
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        s = Session(b, pw, P, url); await s.start(); obs = await s.observe()
        print("ELEMENTS:", *[prompts.fmt_element(e) for e in obs["elements"]], sep="\n  ")
        system = prompts.actor_system(P, goal, [], url, test_identity(P, "dbg0"))
        user = prompts.actor_user(P, {"step": 1}, obs, [])
        for i in range(3):
            raw = await llm.json("actor", system, user, max_tokens=600)
            print(f"\nRAW {i}:", json.dumps(raw)[:400])
            a = clean_actor(raw, P.engine == "integrity", obs)
            print("PARSED:", a.action, a.target, a.value, a.fields)
        await b.close()
asyncio.run(main())
