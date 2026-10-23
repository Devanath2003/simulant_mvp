"""Lists reachable models per provider (first key only). Never prints keys."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx

from backend.config import BASE_URLS, KEYS

print({p: len(k) for p, k in KEYS.items()}, "keys loaded")
for prov, keys in KEYS.items():
    if not keys:
        continue
    try:
        r = httpx.get(BASE_URLS[prov].rstrip("/") + "/models", headers={"Authorization": f"Bearer {keys[0]}"}, timeout=30)
        data = r.json().get("data", [])
        ids = sorted(m["id"] for m in data)
        flt = ids if prov != "nvidia" else [i for i in ids if any(s in i.lower() for s in ("nemotron", "llama-3.2", "gemma", "paligemma", "gpt-oss"))]
        if prov == "gemini":
            flt = [i for i in ids if "gemma" in i.lower() or "flash" in i.lower()]
        print(f"\n[{prov}] status={r.status_code} total={len(ids)}")
        print("\n".join("  " + i for i in flt))
    except Exception as e:
        print(f"[{prov}] ERROR {type(e).__name__}: {e}")
