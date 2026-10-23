# Simulant MVP

Simulated beta users (personas) browse your website like real people and report what confuses, slows or breaks them,
and what an opportunistic user could exploit.

## Run

```powershell
pip install -r requirements.txt
playwright install chromium
python serve.py --demo               # UI at http://127.0.0.1:8000 + flawed demo shop at http://127.0.0.1:8001
python serve.py                      # UI only, to test your own site (it must already be running)
```

Live progress of every run (goals, each step, findings, final score) is printed in this terminal.
If the URL cannot be reached, the run is refused with a clear message instead of starting.

`.env` needs `GROQ_API_1..n`, `NVIDIA_API_1..n`, `GEMINI_API_1..n` (extra keys are rotated automatically).
Terminal-only run: `python scripts/run_cli.py http://127.0.0.1:8001 senior,mobile_3g,deal_hunter`.
Every run is saved to `runs/<id>/` (events.jsonl, screenshots, report.json) and can be replayed from the UI.

## How it works

```
UI ──POST /api/runs──► FastAPI ──► Run ──► 1 Chromium, 1 isolated context per persona (device, zoom, network throttling)
 ▲                                          │
 └──── WebSocket events ◄───────────────────┘   each persona = LangGraph loop:  observe → decide → execute → observe …
```

| Piece | What it does | Model (fallback order) |
|---|---|---|
| Actor (`decide`) | picks the next action in character, from a numbered element list | Groq gpt-oss-120b → gpt-oss-20b → Nemotron Ultra |
| Perceiver | reviews each new page's screenshot as the persona (background task) | Gemma 4 26B → Gemma 4 31B → Nemotron Nano Omni |
| Grounder | Set-of-Marks: finds elements the DOM cannot describe (action `look`) | same as Perceiver |
| Planner / Judge / Reporter | goal per persona, business-rule judge, final report | Groq gpt-oss-120b → Nemotron Ultra → gpt-oss-20b |
| Sensors (no LLM) | a11y audit, load time, console/HTTP errors, overlay-blocked clicks, no-feedback clicks, rage-click duplicate POSTs | – |

## Measured on the local BuggyShop (10 planted bugs)

3 personas run in parallel in ~85-220 s. Typical result: 8 of 10 planted bugs found (icon-only cart, low contrast, cookie banner
covering buttons, jargon + no loading state, negative quantity, tampered price, order page reachable without paying,
duplicate orders from repeat clicks). Usually missed: coupon stacking and the vague "E422" error text.
The Integrity Engine is LLM-driven, so run it twice if a specific abuse matters. Gemma 4 on the Gemini API returns random 500s,
which is why Nemotron Nano Omni sits behind it. LLM usage and error counts per model are in every `report.json`.

Personas live in `backend/personas/*.yaml` (device, zoom, network, patience, abandon threshold, traits).
The Integrity Engine (`engine: integrity`) additionally gets `tamper`, `repeat` and `goto` actions, only runs when the
tester ticks "I own / am authorised to test this site", and cannot navigate off the site's origin.
