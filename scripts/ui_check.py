"""Drives the real UI in a headless browser: start a run, wait for the report, screenshot."""
import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path(sys.argv[1])
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8000/")
    pg.wait_for_selector("#personas input")
    print("persona checkboxes:", pg.locator("#personas input").count())
    # integrity persona must be refused without the authorisation tick
    pg.click("#go"); pg.wait_for_timeout(800)
    print("status without tick:", pg.inner_text("#status")[:120])
    pg.check("#auth"); pg.click("#go")
    pg.wait_for_selector(".pcol", timeout=15000)
    print("columns:", pg.locator(".pcol").count())
    t0 = time.time()
    pg.wait_for_timeout(25000)
    pg.screenshot(path=str(out / "ui_live.png"))
    pg.wait_for_selector("#report:not(.hidden)", timeout=420000)
    print(f"report visible after {time.time()-t0+25:.0f}s; findings rendered:", pg.locator(".finding").count())
    pg.screenshot(path=str(out / "ui_report.png"), full_page=True)
    print("status:", pg.inner_text("#status"))
    print("JS errors:", errors[:5])
    b.close()
