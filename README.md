# Simulant

**Simulated beta users for your website.** Simulant sends AI personas through a site in a real browser, each with its own
device, network and patience. They try to get real tasks done, and you get a liability report of where they got confused,
slowed down or found a loophole, with the evidence for every finding.

Repository: https://github.com/Devanath2003/simulant_mvp

---

## Project overview

Simulant is a working MVP built for the Young Innovators Programme (K-DISC) by Team Glitched Inn, College of Engineering
Karunagappally. You give it a URL and, optionally, a task in plain English ("create a new account", "add any book to the
cart"). It then:

1. **Plans** a realistic goal for each persona.
2. **Runs** every persona at the same time in its own browser: a desktop at 140% zoom, an iPhone on throttled 3G, and so on.
3. **Observes** each step: what changed on screen, how long it took, what the browser measured, and how frustrated the persona got.
4. **Reports** a scored liability report: a verdict, a score per vital, every journey step by step, and deduplicated
   findings with screenshots, the persona's own words and a suggested fix. The report is viewable in the dashboard and
   downloadable as a PDF.

No test scripts, selectors or recordings are needed.

## The idea

**The problem.** Teams ship without ever watching a real novice, a commuter on a bad connection, or an opportunist use
their product. Scripted end-to-end tests only check the paths someone thought to write down. Manual beta testing is
slow, and real users rarely file polite bug reports. They simply leave.

**Our approach.** Model the people instead of the paths. Each persona has a body (device, zoom, network), a temperament
(patience, how often it re-taps, when it quits) and a goal. It reads the page the way a person does and acts in a real
browser. Its frustration rises with silence after a click, confusing screens and vague errors. When it passes the
persona's threshold, the persona leaves, and that becomes a measured drop-off.

**Two engines in one run**

| Engine | Persona | Finds |
|---|---|---|
| **Empathy engine** | Ravi, 62, first-time online shopper (desktop, 140% zoom, 7 s patience) · Priya, 27, commuter (iPhone 13, slow 3G, 2 s patience, re-taps) | clicks with no visible response, buttons covered by banners, blocked form submits, slow pages on the persona's own network, accessibility barriers, confusing screens |
| **Integrity engine** | The Deal-Hunter, an opportunistic user | business-logic abuse: coupon reuse and stacking, negative or absurd quantities, edited hidden price fields, skipping payment, double submissions |

**Evidence, not opinions.** Every finding is labelled by how it was found:
- **Measured:** deterministic browser checks such as timing against the persona's patience, overlays intercepting clicks, contrast ratios, missing labels, failed requests and duplicate POSTs.
- **Rule check:** a strict business-rule judge comparing the page before and after an action.
- **AI-observed:** a vision model's opinion of a screen. These are capped at medium severity unless a second persona agrees.

Mistakes made by our own navigator are kept out of the findings and the score, and listed separately as run diagnostics.

**Responsible use.** The Integrity engine only runs after the tester confirms they own or are authorised to test the site,
and a guard keeps every persona on the site under test.

## Tech stack and tools

| Layer | Tools |
|---|---|
| Language | Python 3.12, vanilla JavaScript |
| Backend API | FastAPI, Uvicorn, WebSockets (live event stream), Pydantic |
| Browser automation | Playwright (Chromium): device emulation, browser zoom, Chrome DevTools Protocol network throttling, request/console capture |
| Agent loop | LangGraph state graph per persona: observe → decide → act → feel |
| Page understanding | injected JavaScript: interactive-element extraction (with clipping and overlay detection), Set-of-Marks numbered screenshots, accessibility and mobile audit, form-validation detection |
| LLM access | OpenAI Python SDK against OpenAI-compatible endpoints, httpx for the Gemini API; one gateway with per-role fallback chains, API-key rotation, cooldowns and a circuit breaker |
| Reports | JSON and JSONL event logs per run, HTML report rendered to PDF by headless Chromium |
| Frontend | plain HTML, CSS and JS, no build step; editorial design system in `DESIGN.md` (Instrument Serif, IBM Plex Mono) |
| Config | YAML persona definitions, `.env` for keys |
| Test target | BuggyShop, a local FastAPI shop with 10 deliberately planted UX and logic bugs (`target_site/`) |

**Models (each role falls back to the next if a model fails or is rate-limited)**

| Role | Models | Used |
|---|---|---|
| Persona brain (decides each step) | gpt-oss-120b on Groq → gpt-6-luna (OpenAI) → gpt-oss-20b → Nemotron 3 Ultra (NVIDIA) | every step |
| Vision navigator | gpt-6-luna → gpt-6.1-sol, with a numbered screenshot | only when navigation goes wrong; capped per run |
| Screen reviewer | Gemma 4 (Gemini API) → Nemotron Nano Omni | each new page, in the background |
| Planner, business-rule judge, report writer | gpt-oss-120b → Nemotron 3 Ultra → gpt-oss-20b | a few calls per run |

Paid OpenAI calls are capped per run (default 60). A three-persona run cost $0.002 to $0.02 in our tests, and the exact
tokens and cost are shown in every report. The Deal-Hunter never uses OpenAI models, because they stop attempting abuses.

## Results on our test shop

Three personas run in parallel in roughly 1.5 to 3 minutes and typically find **8 of the 10 planted bugs**: the icon-only cart,
low-contrast text, a cookie banner covering buttons, jargon with no loading state, negative quantities, a tampered price,
an order page reachable without paying, and duplicate orders from repeat clicks. Coupon stacking and a vague error code
are usually missed. Simulant has also completed real flows on public demo sites (sign-up on automationexercise.com,
login on saucedemo.com, add-to-cart on demowebshop.tricentis.com and demoblaze.com).

## Getting started

```powershell
pip install -r requirements.txt
playwright install chromium
python serve.py --demo     # dashboard at http://127.0.0.1:8000 + the flawed demo shop at http://127.0.0.1:8001
python serve.py            # dashboard only, to test your own site
```

Create a `.env` file with your keys (extra keys of the same provider are rotated automatically):

```
GROQ_API_1=...
NVIDIA_API_1=...
GEMINI_API_1=...
OPEN_AI=...                     # optional: enables the vision navigator
OPENAI_MAX_CALLS_PER_RUN=60     # optional cap on paid calls per run
```

- `http://127.0.0.1:8000/`: landing page
- `http://127.0.0.1:8000/app`: dashboard with run history, a new-simulation form, live journeys and the report (with PDF export)
- Terminal only: `python scripts/run_cli.py <url> senior,mobile_3g,deal_hunter "optional task"`

Every run is saved to `runs/<id>/` (event log, screenshots, `report.json`, `report.pdf`), with a copy of the PDF in
`reports/`. Live progress is printed in the terminal.

## Project structure

```
backend/
  app.py              FastAPI routes, WebSocket stream, report and PDF endpoints
  runner.py           starts a run: one browser, the personas in parallel, then the report
  llm/gateway.py      model chains, key rotation, paid-call budget, token accounting
  navigator/          the persona agent: LangGraph loop, browser session, page scripts, sensors, prompts
  personas/*.yaml     Ravi, Priya, the Deal-Hunter
  report/             deduplication, scoring, narrative, PDF
frontend/             landing page and dashboard (HTML, CSS, JS)
target_site/          BuggyShop, the local test shop with planted bugs
scripts/              terminal runner and model checks
serve.py              starts the dashboard (and the demo shop with --demo)
```

## Team

Team Glitched Inn, College of Engineering Karunagappally · Young Innovators Programme, K-DISC
