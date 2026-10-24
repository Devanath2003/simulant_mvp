"""Printable report: one self-contained HTML page (screenshots inlined) rendered to PDF by headless Chromium."""
import base64
import html
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from backend.config import ROOT

REPORTS_DIR = ROOT / "reports"
SEV_COLOR = {"critical": "#b0271f", "high": "#f08a4b", "medium": "#866608", "low": "#3f63c7"}
SEV_TINT = {"critical": "#ecd7d3", "high": "#f6e1d3", "medium": "#e9e1d4", "low": "#e0e3ee"}   # ~14% of SEV_COLOR on parchment
SOURCE = {"sensor": "measured", "oracle": "rule check", "llm": "AI-observed"}

CSS = """
html, body { background: #f6f3f1; }
@page { size: A4; margin: 0; }
.paper { position: fixed; inset: 0; background: #f6f3f1; z-index: -1; }
table.page { width: 100%; border-collapse: collapse; } table.page > thead td, table.page > tfoot td { height: 15mm; border: 0; padding: 0; }
table.page > tbody > tr > td { padding: 0 14mm; border: 0; }
body { font: 400 10.5px/1.45 'IBM Plex Mono', ui-monospace, Consolas, monospace; color: #242424; margin: 0; letter-spacing: -0.02em; }
h1, h2, h3, .t, .serif { font-family: 'Instrument Serif', Georgia, 'Times New Roman', serif; font-weight: 400; letter-spacing: -0.02em; }
h1 { font-size: 38px; line-height: 1.05; margin: 6px 0 10px; }
h2 { font-size: 22px; margin: 26px 0 8px; padding-bottom: 6px; border-bottom: 1px solid #cecac8; break-after: avoid; page-break-after: avoid; }
.eyebrow, .label { font-size: 8.5px; text-transform: uppercase; color: #797776; letter-spacing: 0; }
.meta { color: #797776; margin-bottom: 12px; }
.advice { display: inline-block; padding: 3px 10px; border-radius: 99px; background: #242424; color: #f6f3f1; font-size: 9px; text-transform: uppercase; }
.panel { display: flex; border: 1px solid #cecac8; border-radius: 18px; margin: 14px 0; overflow: hidden; }
.hero { width: 34%; padding: 16px 18px; border-right: 1px solid #cecac8; }
.hero b { display: block; font: 500 54px/1 'IBM Plex Mono', monospace; letter-spacing: -0.05em; margin-top: 8px; }
.hero b small { font-size: 14px; color: #797776; }
.vit { flex: 1; padding: 14px 18px; }
.vit .r { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
.vit .r span:first-child { width: 90px; text-transform: capitalize; } .vit .r .val { width: 26px; text-align: right; font-weight: 500; }
.bar { flex: 1; height: 6px; border-radius: 6px; background: #d5eadc; overflow: hidden; } .bar i { display: block; height: 100%; background: #2c7a4b; border-radius: 6px; }
.bar.warn { background: #efe5c4; } .bar.warn i { background: #866608; } .bar.bad { background: #f2d6d3; } .bar.bad i { background: #b0271f; }
.summary { font: 400 17px/1.3 'Instrument Serif', Georgia, serif; letter-spacing: -0.01em; margin: 8px 0; }
ul, ol { margin: 4px 0 4px 16px; padding: 0; } li { margin: 3px 0; }
table { border-collapse: collapse; width: 100%; } td, th { border-bottom: 1px solid #cecac8; padding: 5px 6px; text-align: left; vertical-align: top; }
th { font-weight: 400; font-size: 8.5px; text-transform: uppercase; color: #797776; }
.hero { background: #e4efe7; } .hero.warn { background: #f2ebdb; } .hero.bad { background: #f4e5e2; }
.risks { background: #ecded4; border-radius: 14px; padding: 4px 16px 8px; margin-top: 10px; }
.risks h2 { border-bottom-color: rgba(123, 47, 0, .2); margin-top: 10px; } .risks li::marker { color: #7b2f00; }
.f { border: 1px solid #cecac8; border-radius: 12px; margin: 8px 0; page-break-inside: avoid; overflow: hidden; }
.fh { display: flex; align-items: center; gap: 10px; padding: 6px 12px; border-bottom: 1px solid #cecac8; font-size: 8.5px; text-transform: uppercase; color: #797776; }
.fh .w { margin-left: auto; text-transform: none; }
.fb { display: flex; gap: 14px; padding: 8px 12px 10px; } .fb .body { flex: 1; }
.fb img { width: 150px; max-height: 200px; object-fit: cover; object-position: top; border: 1px solid #cecac8; border-radius: 8px; }
.pill { display: inline-flex; align-items: center; gap: 5px; border-radius: 99px; padding: 2px 8px; font-size: 8.5px; text-transform: uppercase; color: #242424; }
.pill i { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.t { font-size: 16px; margin: 2px 0 6px; }
.blk { display: flex; gap: 10px; padding: 4px 0; border-top: 1px solid #e3dfdc; } .blk .label { width: 74px; flex: none; padding-top: 1px; }
.d { color: #4e4d4d; } .q { font: italic 400 13px/1.3 'Instrument Serif', Georgia, serif; }
.fx { margin-top: 4px; background: #e4efe7; border-radius: 8px; padding: 5px 8px; border: 0; } .fx .label { color: #22603b; } .hero .label, .hero b small { color: #4e4d4d; }
.tl { font-size: 9px; color: #4e4d4d; } .tl td { padding: 3px 4px; border-bottom: 1px dashed #cecac8; }
h3 { font-size: 15px; margin: 12px 0 4px; }
.foot { margin-top: 16px; color: #797776; font-size: 8.5px; }
"""


def _e(s) -> str:
    return html.escape(str(s or ""))


def _img(run_dir: Path, rel: str) -> str:
    p = run_dir / rel if rel else None
    if not p or not p.exists():
        return ""
    return f'<img src="data:image/jpeg;base64,{base64.b64encode(p.read_bytes()).decode()}">'


def _where(url: str) -> str:
    u = urlparse(url)
    return (u.netloc.removeprefix("www.") + ("" if u.path in ("", "/") else u.path)) if u.netloc else url


def _nav_err(t: dict) -> bool:
    """A step that failed because of our navigator (bad id, malformed action), not because of the site."""
    if "agent_error" in t:
        return t["agent_error"]
    res = t.get("result", "")
    return res.startswith("FAILED:") and "covering" not in res and "disabled" not in res


def _names() -> dict:
    from backend.runner import load_personas   # late import: runner imports this module
    return {k: p.name.split(" - ")[0] for k, p in load_personas().items()}


def render_html(r: dict, run_dir: Path) -> str:
    sc = r["score"]
    nm = _names()
    risks = "".join(f"<li>{_e(t)}</li>" for t in r.get("top_risks", []))
    journeys = "".join(
        f"<tr><td><b>{_e(nm.get(j['persona'], j['persona']))}</b></td><td>{_e(j['status'])}</td><td>{j['steps']}</td><td>{j['frustration']}/100</td>"
        f"<td>{_e(j['goal'])}<br><i>{_e(j['reason'])}</i></td></tr>" for j in r["journeys"])
    findings = "".join(
        f'<div class="f"><div class="fh"><span class="pill" style="background:{SEV_TINT[f["severity"]]}"><i style="background:{SEV_COLOR[f["severity"]]}"></i>{f["severity"]}</span>'
        f'<span>{_e(f["id"])}</span><span>{_e(f["vital"])} · {SOURCE.get(f["source"], f["source"])} · {_e(", ".join(nm.get(p, p) for p in f["personas"]))}</span>'
        + (f'<span class="w">{_e(_where(f["url"]))}</span>' if f.get("url", "").startswith("http") else "")
        + f'</div><div class="fb"><div class="body"><div class="t">{_e(f["title"])}</div>'
        + (f'<div class="blk"><span class="label">Evidence</span><span class="d">{_e(f["detail"])}</span></div>' if f.get("detail") else "")
        + (f'<div class="blk"><span class="label">In their words</span><span class="q">“{_e(f["persona_quote"])}”</span></div>' if f.get("persona_quote") else "")
        + (f'<div class="blk fx"><span class="label">Suggested fix</span><span>{_e(f["suggested_fix"])}</span></div>' if f.get("suggested_fix") else "")
        + f'</div>{"" if (f.get("url") or "").startswith("chrome-error:") else _img(run_dir, f.get("screenshot", ""))}</div></div>'
        for f in r["findings"])
    timelines = "".join(
        f"<h3 style='font-size:12px;margin:10px 0 4px'>{_e(nm.get(j['persona'], j['persona']))} —{_e(j['status'])}</h3><table class='tl'>"
        + "".join(f"<tr><td>#{t['step']}</td><td>{_e(t['desc'])}</td><td>{_e(t['result'])}</td><td>F {t['frustration']}</td></tr>"
                  for t in j["timeline"] if not _nav_err(t)) + "</table>"
        + (f"<div class='label' style='margin-top:4px'>{sum(map(_nav_err, j['timeline']))} navigator retries omitted (our agent's mistakes, not the site's)</div>"
           if any(map(_nav_err, j["timeline"])) else "")
        for j in r["journeys"])
    band = lambda v: "" if v >= 80 else "warn" if v >= 60 else "bad"
    vit = "".join(f'<div class="r"><span>{_e(k)}</span><span class="bar {band(v)}"><i style="width:{max(2, v)}%"></i></span><span class="val">{v}</span></div>'
                  for k, v in sc["vitals"].items())
    adv = (r.get("release_advice") or "").split(" - ", 1)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><div class="paper"></div>
<table class="page"><thead><tr><td></td></tr></thead><tfoot><tr><td></td></tr></tfoot><tbody><tr><td>
<div class="eyebrow">simulant · liability report</div>
<h1>{_e(urlparse(r['url']).netloc or r['url'])}</h1>
<div class="meta">{_e(r['url'])} · run {_e(r['run_id'])} · {r['elapsed_s']} s · {len(r['journeys'])} simulated users</div>
<span class="advice">{_e(adv[0])}</span> <span class="d">{_e(adv[1] if len(adv) > 1 else '')}</span>
<div class="panel"><div class="hero {band(min(sc['vitals'].values(), default=sc['overall']))}"><div class="label">Overall score</div><b>{sc['overall']}<small> / 100</small></b></div><div class="vit">{vit}</div></div>
<p class="summary">{_e(r.get('summary', ''))}</p>
{f'<div class="risks"><h2>Top risks</h2><ol>{risks}</ol></div>' if risks else ''}
<h2>Simulated users</h2>
<table><tr><th>Persona</th><th>Outcome</th><th>Steps</th><th>Frustration</th><th>Goal / how it ended</th></tr>{journeys}</table>
<h2>Findings ({len(r['findings'])})</h2>{findings}
<h2>Step-by-step journeys</h2>{timelines}
<div class="foot">Severity: measured = deterministic browser checks; rule check = business-rule oracle; AI-observed = vision/LLM opinion (capped at medium unless several personas agree).</div>
</td></tr></tbody></table></body></html>"""


async def write_pdf(browser, report: dict, run_dir: Path) -> Path:
    """Renders runs/<id>/report.pdf and keeps a copy in reports/ with a readable name."""
    page = await browser.new_page()
    try:
        try:   # web fonts if online; falls back to Georgia / system mono offline
            await page.set_content(render_html(report, run_dir), wait_until="networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        out = run_dir / "report.pdf"
        await page.pdf(path=str(out), format="A4", print_background=True,
                       margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    finally:
        await page.close()
    REPORTS_DIR.mkdir(exist_ok=True)
    host = re.sub(r"[^a-zA-Z0-9.-]", "_", urlparse(report["url"]).netloc)
    shutil.copyfile(out, REPORTS_DIR / f"Simulant_{host}_{report['run_id']}.pdf")
    return out
