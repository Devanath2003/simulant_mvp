"""Dedupe findings, score each vital, ask the Reporter model for the narrative."""
import json
import re
import time

from backend.llm.gateway import STATS, USAGE, LLMError, cost_usd, llm
from backend.models import Finding

SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
PENALTY = {"critical": 25, "high": 15, "medium": 8, "low": 3}
VITALS = ["usability", "accessibility", "performance", "functional", "integrity"]


def _key(f: Finding) -> str:
    if f.category and f.category != "other":
        return f"{f.vital}|{f.category}"
    t = re.sub(r"\d+(\.\d+)?", "#", f.title.lower())
    t = re.sub(r"['\"“”].*?['\"“”]", "'x'", t)
    return f"{f.vital}|{re.sub(r'[^a-z# ]', '', t)[:55].strip()}"


def dedupe(findings: list[Finding]) -> list[dict]:
    merged: dict[str, dict] = {}
    for f in findings:
        k = _key(f)
        if k in merged:
            m = merged[k]
            if f.persona not in m["personas"]:
                m["personas"].append(f.persona)
            if SEV_RANK[f.severity] < SEV_RANK[m["severity"]]:
                m["severity"] = f.severity
            m["occurrences"] += 1
            for fld in ("suggested_fix", "screenshot", "persona_quote"):
                if not m[fld] and getattr(f, fld):
                    m[fld] = getattr(f, fld)
        else:
            d = f.model_dump()
            d.update(personas=[f.persona], occurrences=1)
            merged[k] = d
    out = sorted(merged.values(), key=lambda d: (SEV_RANK[d["severity"]], -len(d["personas"]), d["step"]))
    for i, d in enumerate(out, 1):
        d["id"] = f"F{i}"
    return out


def score(deduped: list[dict]) -> dict:
    """Per vital: 100 * 0.5^(penalty/60). Never saturates at 0, so 10 more findings still change the number."""
    pen = {v: 0.0 for v in VITALS}
    for d in deduped:
        w = 0.5 if (d["source"] == "llm" and len(d["personas"]) == 1) else 1.0   # unconfirmed AI opinions count half
        pen[d["vital"]] += PENALTY[d["severity"]] * w
    per = {v: round(100 * 0.5 ** (pen[v] / 60)) for v in VITALS}
    return {"overall": round(sum(per.values()) / len(per)), "vitals": per}


def _is_soft(d: dict) -> bool:
    """AI-observed, single persona, no category: the noisy kind that gets consolidated and severity-capped."""
    return d["source"] == "llm" and not d["category"] and len(d["personas"]) == 1 and not d["title"].startswith("Drop-off")


async def consolidate_soft(deduped: list[dict]) -> list[dict]:
    soft = [d for d in deduped if _is_soft(d)]
    hard = [d for d in deduped if not _is_soft(d)]
    for d in soft:                                    # unconfirmed AI opinions can never be above medium
        if d["severity"] in ("critical", "high"):
            d["severity"] = "medium"
    if len(soft) <= 5:
        return deduped
    system = "You clean up UX observations made by simulated website users. Respond with one JSON object only."
    user = f"""Already reported from direct measurements (do NOT repeat these topics): {json.dumps([d['title'][:80] for d in hard if d['source'] == 'sensor'][:25])}

Raw AI observations (id, persona, text):
{json.dumps([{"id": d["id"] if d.get("id") else str(i), "persona": d["personas"][0], "text": d["title"][:120], "detail": d["detail"][:140]} for i, d in enumerate(soft)])}

Merge observations describing the same underlying problem into one issue. Drop observations that are vague, contradict each other, complain about missing features/images, or repeat the measured topics above. Return at most 8 issues.
JSON: {{"issues": [{{"title": "<specific, max 14 words>", "vital": "usability|accessibility", "severity": "medium|low", "members": [<ids>], "detail": "<one sentence>", "fix": "<one sentence>"}}]}}"""
    try:
        data = await llm.json("reporter", system, user, max_tokens=2500)
        by_id = {(d.get("id") or str(i)): d for i, d in enumerate(soft)}
        merged = []
        for it in data.get("issues", [])[:8]:
            mem = [by_id[str(m)] for m in it.get("members", []) if str(m) in by_id]
            if not mem or not it.get("title"):
                continue
            first = mem[0]
            personas = sorted({p for m in mem for p in m["personas"]})
            merged.append({**first, "title": str(it["title"])[:110], "detail": str(it.get("detail", ""))[:300],
                           "vital": it.get("vital") if it.get("vital") in ("usability", "accessibility") else first["vital"],
                           "severity": "high" if (len(personas) > 1 and it.get("severity") == "medium") else (it.get("severity") if it.get("severity") in ("medium", "low") else "low"),
                           "personas": personas, "occurrences": sum(m["occurrences"] for m in mem),
                           "suggested_fix": str(it.get("fix", ""))[:240] or first["suggested_fix"]})
        if merged:
            return hard + merged
    except LLMError:
        pass
    keep = sorted(soft, key=lambda d: SEV_RANK[d["severity"]])[:8]      # fallback: just cap the amount
    return hard + keep


async def narrative(deduped: list[dict], results: list[dict], url: str) -> dict:
    top = [{"id": d["id"], "sev": d["severity"], "vital": d["vital"], "title": d["title"][:110], "seen_by": d["personas"],
            "fix_known": bool(d["suggested_fix"])} for d in deduped[:40]]
    journeys = [{"persona": r["persona"], "outcome": r["status"], "why": r.get("reason", "")[:160], "steps": r.get("steps", 0)} for r in results]
    system = "You are a senior QA lead writing a concise report for a developer who is about to release a website to real users. Respond with one JSON object only."
    user = f"""Site tested: {url}
Simulated beta users and their outcomes: {json.dumps(journeys)}
Findings (deduplicated): {json.dumps(top)}
Write JSON: {{"summary": "<3-4 plain sentences: overall verdict, the worst problems, who was hurt>",
 "release_advice": "<ship | fix first | do not ship> - one short reason",
 "top_risks": ["<max 4 short bullets, most important first>"],
 "fixes": {{"<finding id>": "<one-sentence concrete fix>"}}  // only for findings where fix_known is false, max 15
}}"""
    def _ok(d):
        if len(str(d.get("summary", "")).strip()) < 30:
            raise ValueError("summary missing")
    return await llm.json("reporter", system, user, max_tokens=2000, validate=_ok)


async def build_report(run, results: list[dict]) -> dict:
    all_f: list[Finding] = [f for r in results for f in r["findings"]]
    deduped = dedupe(all_f)
    deduped = await consolidate_soft(deduped)
    deduped.sort(key=lambda d: (SEV_RANK[d["severity"]], -len(d["personas"]), d["step"]))
    for i, d in enumerate(deduped, 1):
        d["id"] = f"F{i}"
    sc = score(deduped)
    try:
        nar = await narrative(deduped, results, run.url)
        for fid, fix in (nar.get("fixes") or {}).items():
            for d in deduped:
                if d["id"] == fid and not d["suggested_fix"] and isinstance(fix, str):
                    d["suggested_fix"] = fix[:240]
    except LLMError as e:
        worst = deduped[0]["title"] if deduped else "none"
        nar = {"summary": f"{len(deduped)} issues found. Most severe: {worst}. (AI narrative unavailable: {str(e)[:80]})",
               "release_advice": "fix first" if deduped else "ship", "top_risks": [d["title"] for d in deduped[:3]]}
    journeys = []
    for r in results:
        steps = [e for e in run.events if e.get("persona") == r["persona"] and e["type"] == "action"]
        seen = {e["step"] for e in run.events if e.get("persona") == r["persona"] and e["type"] == "thought" and e.get("eyes")}
        journeys.append({"persona": r["persona"], "status": r["status"], "reason": r.get("reason", ""), "goal": r.get("goal", ""),
                         "steps": r.get("steps", 0), "frustration": r.get("frustration", 0),
                         "timeline": [{"step": e["step"], "desc": e["desc"], "result": e["result"], "frustration": e["frustration"],
                                       "agent_error": bool(e.get("agent_error")), "eyes": e["step"] in seen} for e in steps]})
    return {"run_id": run.id, "url": run.url, "elapsed_s": round(time.time() - run.t0, 1), "score": sc,
            "summary": nar.get("summary", ""), "release_advice": nar.get("release_advice", ""), "top_risks": nar.get("top_risks", []),
            "findings": deduped, "journeys": journeys,
            "llm_usage": {k: {"calls": v["calls"], "fails": v["fails"], "errors": v.get("errors", {}),
                                "recent_errors": v.get("samples", [])} for k, v in STATS.items()},
            "paid_usage": paid_usage(getattr(run, "usage0", {}), run.events)}


def paid_usage(before: dict, events: list) -> dict:
    """This run's OpenAI tokens and cost (exact counts reported by the API), and how often the vision navigator stepped in."""
    models, total = {}, 0.0
    for k, u in USAGE.items():
        if not k.startswith("openai/"):
            continue
        b = before.get(k, {"in": 0, "cached": 0, "out": 0})
        d = {f: u[f] - b[f] for f in ("in", "cached", "out")}
        if d["in"] or d["out"]:
            d["cost_usd"] = round(cost_usd(k, d), 5)
            total += d["cost_usd"]
            models[k.split("/", 1)[1]] = d
    rescues = sum(1 for e in events if e["type"] == "thought" and e.get("eyes"))
    return {"models": models, "cost_usd": round(total, 4), "navigator_rescues": rescues}
