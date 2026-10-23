import asyncio, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.async_api import async_playwright
from backend.llm.gateway import llm, STATS, extract_json

HTML = "<body style='font:20px sans-serif'><h2>Demo Shop</h2><button style='padding:12px'>Add to cart</button> <input placeholder='Coupon code'></body>"

# unit checks for the JSON extractor
assert extract_json('<thought>x {"a":1}</thought> ```json\n{"a": 2,}\n```')["a"] == 2
assert extract_json('blah {"s": "a } b \\" c", "n": {"k": 1}} tail')["n"]["k"] == 1
print("extract_json ok")

async def run(role, label, **kw):
    t = time.time()
    try:
        d = await llm.json(role, kw.pop("system"), kw.pop("user"), **kw)
        print(f"OK  {label:10s} {time.time()-t:5.1f}s {str(d)[:140]}", flush=True)
    except Exception as e:
        print(f"ERR {label:10s} {time.time()-t:5.1f}s {str(e)[:300]}", flush=True)

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={"width": 700, "height": 300})
        await pg.set_content(HTML); jpg = await pg.screenshot(type="jpeg", quality=60); await b.close()
    sysm = "You answer with JSON only."
    await asyncio.gather(
        run("actor", "actor", system=sysm, user='Return {"action":"click","target":1}. Elements: [1] button "Add to cart"'),
        run("actor", "actor2", system=sysm, user='Return {"action":"type","target":2,"value":"SAVE10"}. Elements: [2] textbox "Coupon"'),
        run("planner", "planner", system=sysm, user='Return {"goal": "<one sentence shopping goal for a 62 year old>"}', max_tokens=300),
        run("vision", "vision", system=sysm, user='Return {"elements":[strings],"issues":[strings]} for this page.', image=jpg),
        run("vision", "vision2", system=sysm, user='Return {"elements":[strings]} for this page.', image=jpg),
    )
    print({k: v for k, v in STATS.items()})
asyncio.run(main())
