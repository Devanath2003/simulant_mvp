"""Deterministic detectors. They cost no LLM tokens, so their findings are the most trustworthy ones."""
import re
from urllib.parse import urlparse

from backend.models import Finding, Persona

FIX = {
    "Images without alt text": "Add a meaningful alt attribute to every informative image (alt=\"\" for decorative ones).",
    "Form fields without a visible label": "Add a <label for=...> to each field; do not rely on placeholder text alone.",
    "Buttons/links with no accessible name": "Give icon-only controls a visible text label or at least an aria-label.",
    "Page has no lang attribute": "Set <html lang=\"en\"> (or the page language).",
    "Page has no <h1>": "Add one descriptive <h1> per page.",
    "Pinch-zoom disabled": "Remove user-scalable=no / maximum-scale=1 from the viewport meta tag.",
    "Low colour contrast": "Raise text/background contrast to at least 4.5:1 (3:1 for large text).",
    "Tap targets smaller": "Make touch targets at least 44x44px with spacing between them.",
    "Text smaller than 12px": "Use at least 14-16px for body text.",
    "Page scrolls sideways": "Fix fixed-width elements so the layout fits the viewport width.",
}
MOBILE_ONLY = ("Tap targets", "Page scrolls sideways")


def _fix_for(title: str) -> str:
    return next((v for k, v in FIX.items() if title.startswith(k)), "")


def audit_findings(persona: Persona, url: str, issues: list, step: int, shot: str) -> list[Finding]:
    mobile = persona.device != "desktop"
    out = []
    for i in issues:
        if i["kind"].startswith(MOBILE_ONLY) and not mobile:
            continue
        sev = i["severity"]
        if persona.id == "senior" and i["kind"].startswith(("Low colour", "Text smaller")):
            sev = "high" if sev == "medium" else "medium"      # matters more for this persona
        out.append(Finding(
            persona=persona.id, engine=persona.engine, vital="accessibility", severity=sev,
            title=f"{i['kind']} ({i['count']})", source="sensor", url=url, step=step, screenshot=shot,
            detail="Examples: " + "; ".join(i["sample"]), suggested_fix=_fix_for(i["kind"])))
    return out


def load_time_finding(persona: Persona, url: str, ms: int, step: int, shot: str) -> list[Finding]:
    limit = 3000 if persona.network == "slow_3g" else 2500
    if ms < limit:
        return []
    sev = "high" if ms > 2 * limit else "medium"
    net = "on a slow 3G connection" if persona.network == "slow_3g" else "on a normal connection"
    return [Finding(persona=persona.id, engine=persona.engine, vital="performance", severity=sev,
                    title=f"Slow page load: {ms / 1000:.1f}s {net}", source="sensor", url=url, step=step, screenshot=shot,
                    detail=f"{urlparse(url).path or '/'} took {ms} ms to finish loading.",
                    suggested_fix="Compress images, defer non-critical scripts and cache static assets.")]


def runtime_findings(persona: Persona, console_errors: list[str], http_errors: list[dict]) -> list[Finding]:
    out, seen = [], set()
    for e in console_errors:
        key = re.sub(r"\d+", "#", e)[:80]
        if key in seen or "favicon" in e:
            continue
        seen.add(key)
        out.append(Finding(persona=persona.id, engine=persona.engine, vital="functional", severity="medium",
                           title="JavaScript/console error: " + e[:90], source="sensor", detail=e[:300],
                           suggested_fix="Fix the script error; it can silently break page features."))
    for h in http_errors:
        key = (h["status"], urlparse(h["url"]).path)
        if key in seen:
            continue
        seen.add(key)
        sev = "high" if h["status"] >= 500 else "low"
        out.append(Finding(persona=persona.id, engine=persona.engine, vital="functional", severity=sev,
                           title=f"HTTP {h['status']} from {h['method']} {urlparse(h['url']).path}", source="sensor",
                           url=h["url"], detail="A request made by the page failed.",
                           suggested_fix="Check the server logs for this endpoint and handle the failure gracefully in the UI."))
    return out
