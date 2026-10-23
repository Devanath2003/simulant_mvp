"""Start Simulant:  python serve.py           ->  UI at http://127.0.0.1:8000
                   python serve.py --demo    ->  also starts the flawed demo shop at http://127.0.0.1:8001
(No --reload on purpose: on Windows the reloader uses a loop that cannot spawn Playwright browsers.)"""
import subprocess
import sys

import uvicorn

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp1252
    shop = None
    if "--demo" in sys.argv:
        shop = subprocess.Popen([sys.executable, "-m", "uvicorn", "target_site.app:app", "--port", "8001", "--log-level", "warning"])
        print("Demo shop (BuggyShop):  http://127.0.0.1:8001")
    print("Simulant UI:            http://127.0.0.1:8000   (run progress is printed here)\n", flush=True)
    try:
        uvicorn.run("backend.app:app", host="127.0.0.1", port=8000, log_level="warning")
    finally:
        if shop:
            shop.terminate()
