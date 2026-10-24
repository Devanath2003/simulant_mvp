"""All prompts in one place so they are easy to tune."""
from backend.models import Persona

ACTIONS_COMMON = """\
click    {"action":"click","target":ID}
type     {"action":"type","target":ID,"value":"text"}      (add \\n at the end ONLY if you want to press Enter to submit, e.g. a search box; not for ordinary form fields)
fill     {"action":"fill","fields":[{"target":ID,"value":"text"}, ...]}   (fill SEVERAL fields of a form in one step: text boxes, dropdowns (value = option text) and checkboxes/radios (value = "check"))
select   {"action":"select","target":ID,"value":"option text"}
scroll   {"action":"scroll","value":"down"|"up"}
back     {"action":"back"}
wait     {"action":"wait","value":"seconds"}                (e.g. while something loads)
look     {"action":"look","value":"describe the thing you cannot find in the list"}   (a vision helper locates it on the screenshot)
done     {"action":"done","success":true|false}             (goal reached, or you decided it is impossible)
give_up  {"action":"give_up"}                               (you would abandon this site now because it is too confusing, slow or frustrating - only when your frustration is already high)"""

ACTIONS_INTEGRITY = """
tamper   {"action":"tamper","target":ID,"value":"new value"}   (force-edit ANY field incl. hidden/readonly ones, e.g. a price)
repeat   {"action":"repeat","target":ID,"value":"3"}           (click the same control N times very fast)
goto     {"action":"goto","value":"/path"}                      (open a URL on this site directly, skipping steps; confirmation pages are often at /success, /thank-you, /confirmation, /order-confirmation)
fill_form {"action":"fill_form"}                                (fills every EMPTY plain text field on the page with plausible valid data - use it on checkout/signup forms)"""


ATTACKS = {
    "coupon_reuse": "apply the same coupon code twice",
    "coupon_stack": "apply several different/stacked coupons",
    "qty_negative": "set a quantity to a negative number",
    "qty_zero": "set a quantity to 0",
    "qty_huge": "set an absurd quantity (e.g. 999999)",
    "price_tampering": "force-edit a hidden/readonly price or total field, then submit",
    "skip_to_confirmation": "open the order-confirmation page directly (try /success, /thank-you, /confirmation) without paying",
    "double_submit": "click the pay/place-order button several times quickly",
    "back_resubmit": "press back after paying and submit again",
}


def identity_text(idt: dict) -> str:
    return ("\nYOUR DETAILS (use exactly these whenever the site asks for personal data, e.g. sign-up or checkout; never real card numbers): "
            + "; ".join(f"{k}: {v}" for k, v in idt.items())) if idt else ""


def actor_system(p: Persona, goal: str, focus: list, site: str, identity: dict = None) -> str:
    integrity = p.engine == "integrity"
    attack_field = (' "attack": "<one of: ' + " | ".join(ATTACKS) + ' - the abuse idea this action belongs to, or empty for normal navigation>",\n'
                    if integrity else "")
    who = ("You are playing an OPPORTUNISTIC user who looks for loopholes in a website you are authorised to test."
           if integrity else
           "You are role-playing a real person using a website for the first time. You are NOT a tester: behave exactly like this person would, including their limits and bad habits.")
    label = "Things worth trying on this site" if integrity else "Suggested path (follow it, in your own way)"
    focus_txt = (f"\n{label}: " + "; ".join(focus)) if focus else ""
    return f"""{who}

PERSONA: {p.name}. {p.description}
Habits and traits:
{p.traits}

SITE: {site}
YOUR GOAL: {goal}{focus_txt}{identity_text(identity)}

Each turn you get the current page: visible text, alerts, and a numbered list of interactive elements. Choose exactly ONE action.
Element flags: icon-only = no text label; low_contrast = text hard to read; small = tiny target; COVERED = something overlaps it; offscreen = outside the visible area (you can still click it directly; scrolling is normal and never a problem); disabled = cannot be used.
Forms: fill all the fields you need with ONE fill action, then click the submit button. If the browser says a field is required or invalid, fix exactly that field.
Finding things: like any regular web user, if what you need is not in the list, open the site's menu (a "Menu"/"Categories" button or a ☰ icon - on phones the navigation usually hides there) or type what you want into the site's search box. If a click fails or does nothing twice, try one of these other routes instead of repeating it.

ACTIONS:
{ACTIONS_COMMON}{ACTIONS_INTEGRITY if integrity else ""}

Respond with ONE JSON object only:
{{"thought": "<first person, in character, max 25 words>",
 "action": "...", "target": <id or null>, "value": <string or null>,
 "confusion": <0-10, how confused/annoyed this screen makes YOU>,
{attack_field} "ux_note": "<ONE concrete, observable problem on this screen from your persona's view (unlabeled icon, unreadable text, vague error, nothing happened, banner in the way, slow). Empty string if none. Never invent problems; needing to scroll or a long form is NOT a problem.>",
 "memory": "<your running notes, max 40 words{": baseline price, what you already tried and the totals before/after" if integrity else ": what you have done and what remains"}>"}}
Only use element ids that appear in the current list. Do not invent extra requirements beyond YOUR GOAL (e.g. payment methods or features the site does not show). Typing into a field changes nothing on the server until you click its submit/Update/Apply/Pay button, and forms usually reset after a submit (re-enter values before submitting again). When the goal is finished (e.g. an order confirmation is shown), use done with success true."""


def fmt_element(e: dict) -> str:
    name = e["name"] or ("(no text - icon only)" if e.get("icon_only") else "(no label)")
    s = f'[{e["id"]}] {e["type"]} "{name}"'
    if e.get("value") not in (None, ""):
        s += f' value="{e["value"]}"'
    if e.get("options"):
        s += " options=" + "|".join(e["options"])
    flags = [k for k in ("disabled", "checked", "required", "readonly", "offscreen", "small") if e.get(k)]
    if e.get("icon_only"):
        flags.append("icon-only")
    if e.get("low_contrast"):
        flags.append(f"low_contrast({e['low_contrast']})")
    if e.get("covered"):
        flags.append("COVERED by " + e["covered"])
    if e.get("form"):
        flags.append(f'in form "{e["form"]}"')
    return s + (" {" + ", ".join(flags) + "}" if flags else "")


def actor_user(p: Persona, s: dict, obs: dict, warnings: list, visited: list = (), goal: str = "") -> str:
    # repeated last, where the decision is made: personas otherwise drift past the goal (cart -> checkout -> payment)
    done_check = ("" if p.engine == "integrity" or not goal else
                  f"\n\nYOUR GOAL: {goal}\nIf the screen or your last result shows this goal is achieved, answer done with success true now. Do nothing beyond the goal.")
    hist = "\n".join(f"  {i + 1}. {h}" for i, h in enumerate(s.get("history", [])[-5:])) or "  (none yet)"
    els = "\n".join(fmt_element(e) for e in obs["elements"]) or "(no interactive elements found)"
    more = obs.get("more") or {}
    if more.get("below") or more.get("above"):
        els += (f"\n(+{more.get('below', 0)} more interactive elements further DOWN and {more.get('above', 0)} further UP the page"
                " - scroll to bring them into this list)")
    hidden = ""
    if p.engine == "integrity" and obs.get("hidden"):
        hidden = "\nHidden/readonly fields (editable with tamper):\n" + "\n".join(
            f'[{h["id"]}] {h["kind"]} field "{h["name"]}" = "{h["value"]}"' for h in obs["hidden"])
    alerts = ("\nAlerts on screen: " + " | ".join(obs["alerts"])) if obs["alerts"] else ""
    scroll = f" (scrolled {obs['scroll']['y']}px of {obs['scroll']['max']}px)" if obs["scroll"]["max"] > 0 else ""
    warn = ("\nWARNING: " + " ".join(warnings)) if warnings else ""
    seen = ("\nPages visited so far: " + ", ".join(visited[-10:])) if p.engine == "integrity" and visited else ""
    attacks = ""
    if p.engine == "integrity":
        tried = s.get("tried", {})
        todo = [k for k in ATTACKS if k not in tried]
        attacks = ("\nAbuse ideas already attempted (times): " + (", ".join(f"{k} x{v}" for k, v in tried.items()) or "none")
                   + "\nNOT yet attempted: " + (", ".join(f"{k} ({ATTACKS[k]})" for k in todo) or "none - you may finish with done"))
    return f"""Step {s.get('step', 1)} of max {p.max_steps}. Your frustration: {s.get('frustration', 0)}/100 (you quit at {p.abandon_at}).
My notes so far: {s.get('memory') or '(none)'}
Recent actions and what happened:
{hist}{attacks}
{('Last action result: ' + s['last_result']) if s.get('last_result') else ''}{warn}

URL: {obs['url']}{seen}
Page title: {obs['title']}{scroll}
Text currently visible on screen:
\"\"\"
{obs['text']}
\"\"\"{alerts}

Interactive elements:
{els}{hidden}{done_check}"""


# ------------------------------------------------------------------- planner
PLANNER_SYSTEM = "You design realistic test scenarios for simulated website users. Respond with one JSON object only."


def planner_user(p: Persona, obs: dict, user_goal: str) -> str:
    els = "; ".join(f'{e["type"]} "{e["name"]}"' for e in obs["elements"][:25])
    ask = ("Write ONE concrete goal this persona would plausibly try on this site: the site's MAIN flow from start to finish "
           "(for a shop: choose a specific product, add it to the cart, go through checkout and place the order; for other sites: the core task "
           "a first-time visitor came for). Not just browsing or viewing a page. One sentence, doable in about 15 steps. "
           "It MUST be achievable with the features visible on the landing page: do not mention payment methods, accounts, "
           "delivery options, filters or any other feature you cannot see in the page text or controls below."
           if p.engine == "empathy" else
           "Write ONE goal for this opportunistic user: first complete a normal purchase/signup flow up to the point where money/benefits are decided, "
           "then probe business rules. Also list 3-5 specific abuse ideas that fit the features visible on THIS site "
           "(coupon codes, quantities, prices, checkout steps, order/confirmation pages, etc.).")
    if user_goal:   # the site owner's task wins: rephrase it for the persona, never add or drop tasks
        ask = (f'The site owner wants testers to do EXACTLY this task: "{user_goal}". Write the goal as this task in the persona\'s words. '
               "Do NOT add extra tasks (no shopping, cart or checkout unless the task says so) and do not leave anything out."
               + ("" if p.engine == "empathy" else
                  " Then list 3-5 abuse ideas that fit this task on this site (e.g. reusing an email, skipping steps, invalid values)."))
    given = ""
    why = f"\nWhy they came: {p.intent} (adapt this to what the site actually offers)" if (p.intent and not user_goal) else ""
    return f"""Persona: {p.name}. {p.description}{why}
Site title: {obs['title']}  URL: {obs['url']}
Landing page text: {obs['text'][:700]}
Visible controls: {els}{given}
{ask}
Answer as JSON: "goal" = the goal sentence. "focus" = for an empathy persona the ORDERED 3-8 short steps from the landing page to the finished goal (only steps the goal needs); for the integrity persona 3-5 abuse ideas."""


# ------------------------------------------------------------------- vision
PERCEIVER_SYSTEM = ("You are a UX and accessibility reviewer who looks at a screenshot through the eyes of a specific user. "
                    "Report only concrete, visible problems. Respond with one JSON object only.")


def perceiver_user(p: Persona, obs: dict, goal: str) -> str:
    dev = "a phone on a slow mobile connection" if p.device != "desktop" else "a desktop browser"
    zoom = " with enlarged text (browser zoom)" if p.zoom > 1 else ""
    return f"""The user is {p.name}. {p.description} They use {dev}{zoom}. Their goal: {goal}
Page: {obs['title']} ({obs['url']})
Look at the screenshot. List at most 3 CONCRETE problems THIS user would hit, only ones you can point to on the screen: hard-to-read or low-contrast text, unclear icons or labels, jargon, cluttered layout, elements overlapping or hidden behind banners, tiny tap targets, confusing wording. Do NOT complain about missing features, images or content you cannot be sure should exist, and never about needing to scroll or a long form. Judge only how this screen looks and reads, not where the site navigated to (e.g. landing on a product list after login is normal). Skip anything that looks fine; an empty list is a good answer.
JSON: {{"first_impression": "<what this user thinks at first glance, in their voice, max 20 words>",
 "issues": [{{"title": "<specific, max 14 words>", "detail": "<what and where on the screen>", "severity": "high|medium|low", "vital": "usability|accessibility"}}]}}"""


GROUNDER_SYSTEM = ("The screenshot has numbered red boxes drawn over interactive elements. "
                   "Pick the box that best matches the description. Respond with one JSON object only: "
                   '{"target": <number or null if nothing matches>, "reason": "<short>"}')

# ------------------------------------------------------------------- integrity judge
JUDGE_SYSTEM = ("You are a strict QA analyst checking whether a web application enforces its business rules. "
                "You only report a violation when the before/after page text gives concrete evidence. Respond with one JSON object only.")


def judge_user(action: str, thought: str, before: dict, after: dict, requests: list) -> str:
    return f"""An opportunistic user did: {action}
Their intent: {thought}
Requests triggered: {', '.join(requests) or 'none'}

PAGE BEFORE ({before.get('url', '')}):
\"\"\"{before.get('full_text', before.get('text', ''))[:1500]}\"\"\"
PAGE AFTER ({after.get('url', '')}):
\"\"\"{after.get('full_text', after.get('text', ''))[:1500]}\"\"\"
Alerts after: {' | '.join(after.get('alerts', [])) or 'none'}

Did the application FAIL to enforce a sensible business rule? Examples of failures: the same coupon applied twice or several coupons stacked; a negative/zero quantity accepted or the total reduced; a client-edited price accepted; an order/confirmation shown without payment; duplicate orders from repeated submits.
NOT failures: validation errors, rejections, ordinary price changes caused by changing a valid quantity, normal navigation.
Pick the category from exactly this list: coupon_stacking | invalid_quantity | price_tampering | payment_bypass | duplicate_order | other
JSON: {{"violation": true|false, "confidence": <0..1>, "category": "<one of the list>", "severity": "critical|high|medium", "title": "<short, specific>", "rule": "<business rule broken>", "evidence": "<quote the numbers/text before vs after>", "fix": "<one sentence>"}}"""
