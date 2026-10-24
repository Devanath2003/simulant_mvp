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
# the report speaks about people, the technical check name and DOM examples go into the evidence
IMPACT = {
    "Images without alt text": "{n} images are silent to screen-reader users",
    "Form fields without a visible label": "{n} form fields lose their label as soon as you start typing",
    "Buttons/links with no accessible name": "{n} icon buttons and links can't be identified by screen-reader users",
    "Page has no lang attribute": "Screen readers may read this page in the wrong language",
    "Page has no <h1>": "No main heading, so the page is hard to navigate with assistive technology",
    "Pinch-zoom disabled": "Users can't pinch-zoom to read small text",
    "Low colour contrast": "{n} pieces of text are hard to read (low contrast)",
    "Tap targets smaller": "{n} tap targets are too small to hit reliably with a thumb",
    "Text smaller than 12px": "{n} pieces of text are too small to read comfortably",
    "Page scrolls sideways": "The page scrolls sideways on a phone",
}


def _fix_for(title: str) -> str:
    return next((v for k, v in FIX.items() if title.startswith(k)), "")


SINGULAR = {"{n} images are": "An image is", "{n} form fields lose their": "A form field loses its",
            "{n} icon buttons and links": "An icon button", "{n} pieces of text are": "Some text is",
            "{n} tap targets are": "A tap target is"}


def _impact(kind: str, n: int) -> str:
    t = next((v for k, v in IMPACT.items() if kind.startswith(k)), "")
    if not t:
        return f"{kind} ({n})"
    if n == 1:
        t = next((t.replace(p, s) for p, s in SINGULAR.items() if t.startswith(p)), t)
    return t.format(n=n)


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
            title=_impact(i["kind"], i["count"]), source="sensor", url=url, step=step, screenshot=shot,
            category="a11y:" + re.sub(r"[^a-z]+", "_", i["kind"].lower())[:30],
            detail=f"Check: {i['kind']}, {i['count']} on this page. Examples: " + "; ".join(i["sample"]),
            suggested_fix=_fix_for(i["kind"])))
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


def _third_party(text: str, site_host: str) -> bool:
    """True when the error is about another domain (ads, analytics, CDNs) rather than the site under test."""
    base = ".".join(site_host.split(":")[0].split(".")[-2:])
    hosts = re.findall(r"https?://([^/\s'\"()]+)", text)
    return any(not h.split(":")[0].endswith(base) for h in hosts)   # it involves a foreign host (ads, CDNs, analytics)


def runtime_findings(persona: Persona, console_errors: list[str], http_errors: list[dict], site_host: str = "") -> list[Finding]:
    out, seen = [], set()
    third = [e for e in console_errors if site_host and _third_party(e, site_host)]
    third += [f"{h['status']} {h['url']}" for h in http_errors if site_host and _third_party(h["url"], site_host)]
    if third:
        hosts = sorted({h for t in third for h in re.findall(r"https?://([^/\s'\"]+)", t)})
        out.append(Finding(persona=persona.id, engine=persona.engine, vital="functional", severity="low", source="sensor",
                           title=f"Third-party scripts/resources failing ({len(third)} errors)",
                           detail="Hosts: " + ", ".join(hosts[:6]) + ". Usually ads/analytics; check they do not slow or break the page.",
                           suggested_fix="Load third-party scripts async and make the page resilient when they fail or are blocked."))
    console_errors = [e for e in console_errors if not (site_host and _third_party(e, site_host))]
    http_errors = [h for h in http_errors if not (site_host and _third_party(h["url"], site_host))]
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
