"""Playwright session for one persona: device/network emulation, observation, actions, effect measurement."""
import asyncio
import base64
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Error as PWError, Page

from backend.models import ActorOutput, Persona
from backend.navigator.dom import AUDIT_JS, EXTRACT_JS, FILL_JS, VALIDATION_JS, SIGNATURE_JS, SOM_OFF_JS, SOM_ON_JS

# "slow 3G"-ish: 400ms RTT, ~1.2 Mbit/s down, ~600 kbit/s up
SLOW_3G = dict(offline=False, latency=400, downloadThroughput=150 * 1024, uploadThroughput=75 * 1024)
STATIC_EXT = re.compile(r"\.(css|js|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|map)(\?|$)", re.I)


def site_of(url_or_netloc: str) -> str:
    """The site a URL belongs to: www.wikipedia.org and en.wikipedia.org are one site; localhost/IPs keep their port."""
    netloc = urlparse(url_or_netloc).netloc if "//" in url_or_netloc else url_or_netloc
    host = netloc.split(":")[0].lower()
    if host == "localhost" or re.fullmatch(r"[\d.]+", host):
        return netloc.lower()
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") else 2
    return ".".join(labels[-keep:])


@dataclass
class ActionResult:
    ok: bool = True
    error: str = ""
    changed: bool = True
    first_change_s: Optional[float] = None
    settle_s: float = 0.0
    rage_clicks: int = 0
    requests: list = field(default_factory=list)      # ["POST /api/order 200", ...] caused by the action
    duplicate_posts: int = 0
    dialogs: list = field(default_factory=list)
    url_changed: bool = False
    eventually_changed: bool = False   # page differs from before once the network went quiet (even if it was slow)
    kind: str = "click"
    validation: list = field(default_factory=list)    # browser-native "Please fill out this field." bubbles that blocked a submit
    filled: int = 0
    fill_errors: list = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return f"FAILED: {self.error}"
        if self.validation:
            v = self.validation[0]
            others = ", ".join(f"'{x['name']}'" for x in self.validation[1:])
            return (f"the browser blocked the submit and showed the message \"{v['msg']}\" next to '{v['name']}'"
                    + (f" (also not valid yet: {others})" if others else ""))
        if self.kind == "fill":
            return (f"filled {self.filled} field(s) - not submitted yet" + (f"; problems: {'; '.join(self.fill_errors)}" if self.fill_errors else ""))
        if self.kind in ("type", "select", "tamper", "fill_form"):
            return "entered - not submitted yet (nothing is sent until you click the form's submit/Update/Apply/Pay button)"
        if self.kind in ("scroll", "wait"):
            return "done" + (f"; popup said: {self.dialogs[0][:60]}" if self.dialogs else "")
        if not self.changed:
            return "nothing visibly happened" + (f" (even after {self.rage_clicks} extra clicks)" if self.rage_clicks else "")
        s = "page changed" if self.first_change_s is None else f"page changed after {self.first_change_s:.1f}s"
        if self.url_changed:
            s += ", new page"
        if self.dialogs:
            s += f", popup said: {self.dialogs[0][:60]}"
        return s


class Session:
    def __init__(self, browser: Browser, playwright, persona: Persona, start_url: str):
        self.browser, self.pw, self.persona = browser, playwright, persona
        self.start_url = start_url
        self.origin = urlparse(start_url).netloc
        self.site = site_of(start_url)
        self.context = None
        self.page: Optional[Page] = None
        self.net: list[dict] = []          # every request: {t, method, url, type, status}
        self.pending = 0
        self.console_errors: list[str] = []
        self.http_errors: list[dict] = []
        self.dialogs: list[str] = []
        self.load_ms: dict[str, int] = {}  # url -> navigation duration

    # ------------------------------------------------------------ setup
    async def start(self):
        p = self.persona
        if p.device != "desktop" and p.device in self.pw.devices:
            kw = dict(self.pw.devices[p.device])
            kw.pop("default_browser_type", None)
        else:
            z = max(p.zoom, 1.0)   # browser zoom == smaller CSS viewport with higher pixel density
            kw = dict(viewport={"width": round(p.viewport["width"] / z), "height": round(p.viewport["height"] / z)},
                      device_scale_factor=z)
        self.context = await self.browser.new_context(ignore_https_errors=True, **kw)
        self.context.set_default_timeout(8000)
        await self.context.route("**/*", self._guard)
        self.context.on("page", self._on_new_page)
        page = await self.context.new_page()
        self._attach(page)
        if p.network == "slow_3g":
            cdp = await self.context.new_cdp_session(page)
            await cdp.send("Network.emulateNetworkConditions", SLOW_3G)
        await self.goto(self.start_url)

    async def _guard(self, route):
        """Stay on the site under test: block top-level navigation to other sites."""
        req = route.request
        if req.is_navigation_request() and req.url.startswith("http") and site_of(req.url) != self.site:
            await route.abort()
        else:
            await route.continue_()

    def _on_new_page(self, page: Page):
        self._attach(page)
        self.page = page

    def _attach(self, page: Page):
        self.page = page
        page.on("console", lambda m: self.console_errors.append(
            m.text[:200] + (f" (at {m.location.get('url', '')[:120]})" if m.location and m.location.get("url") else "")) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.console_errors.append("Uncaught: " + str(e)[:200]))
        page.on("dialog", lambda d: asyncio.ensure_future(self._on_dialog(d)))
        page.on("request", self._on_request)
        page.on("requestfinished", lambda r: self._done(r))
        page.on("requestfailed", lambda r: self._done(r))
        page.on("response", self._on_response)

    async def _on_dialog(self, d):
        self.dialogs.append(f"{d.type}: {d.message}")
        try:
            await d.accept()
        except PWError:
            pass

    def _on_request(self, r):
        self.pending += 1
        self.net.append({"t": time.time(), "method": r.method, "url": r.url, "type": r.resource_type, "status": None, "_r": r})

    def _done(self, r):
        self.pending = max(0, self.pending - 1)

    def _on_response(self, resp):
        for rec in reversed(self.net[-40:]):
            if rec["_r"] is resp.request:
                rec["status"] = resp.status
                break
        if resp.status >= 400 and not resp.url.endswith("favicon.ico"):
            self.http_errors.append({"status": resp.status, "method": resp.request.method, "url": resp.url})

    # ------------------------------------------------------------ navigation / observation
    def same_site(self, url: str) -> str:
        full = urljoin(self.page.url if self.page else self.start_url, url)
        if site_of(full) != self.site:
            raise ValueError("off-site navigation is not allowed")
        return full

    async def goto(self, url: str):
        full = self.same_site(url)
        await self.page.goto(full, wait_until="domcontentloaded", timeout=25000)
        await self._settle(max_wait=6.0)
        await self._record_load()

    async def _record_load(self):
        try:
            ms = await self.page.evaluate("() => { const n = performance.getEntriesByType('navigation')[0]; return n ? Math.round(n.loadEventEnd || n.domContentLoadedEventEnd || n.duration) : 0; }")
            if ms > 0:
                self.load_ms[self.page.url] = ms
        except PWError:
            pass

    async def _eval(self, js, arg=None, retries=3):
        for i in range(retries):
            try:
                return await self.page.evaluate(js, arg) if arg is not None else await self.page.evaluate(js)
            except PWError as e:
                if i == retries - 1:
                    raise
                await asyncio.sleep(0.4)   # page was navigating

    async def observe(self) -> dict:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PWError:
            pass
        return await self._eval(EXTRACT_JS, {"max": 60, "textMax": 1000})

    async def audit(self) -> list:
        try:
            return await self._eval(AUDIT_JS)
        except PWError:
            return []

    async def screenshot(self, quality: int = 55) -> bytes:
        """JPEG of the viewport; b"" if the page cannot be captured at all (never raises)."""
        try:
            return await self.page.screenshot(type="jpeg", quality=quality, timeout=8000, animations="disabled")
        except PWError:
            # Playwright waits for web fonts first, which can hang on slow networks; CDP grabs what is painted now
            try:
                cdp = await self.context.new_cdp_session(self.page)
                data = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": quality})
                await cdp.detach()
                return base64.b64decode(data["data"])
            except Exception:  # noqa: BLE001
                return b""

    async def som_screenshot(self) -> bytes:
        await self._eval(SOM_ON_JS)
        try:
            jpg = await self.screenshot(quality=70)
            if not jpg:
                raise ValueError("screenshot failed")
            return jpg
        finally:
            try:
                await self._eval(SOM_OFF_JS)
            except PWError:
                pass

    async def signature(self) -> dict:
        try:
            return await self.page.evaluate(SIGNATURE_JS)
        except PWError:
            return {"url": "navigating", "h": time.time()}   # mid-navigation counts as a change

    # ------------------------------------------------------------ actions
    def _human_error(self, e: Exception) -> str:
        m = str(e)
        if "intercepts pointer events" in m:
            snip = re.search(r"<([^>]{0,80})>[^\n]*intercepts pointer events", m)
            return "blocked: another element is covering it" + (f" (<{snip.group(1)[:60]}>)" if snip else "")
        if "not enabled" in m:
            return "the control is disabled"
        if "not visible" in m:
            return "the element is not visible"
        if "Timeout" in m:
            return "could not interact with it (timed out)"
        if "not allowed" in m or '"fill"' in m or "needs" in m:
            return str(e)
        return m.split("\n")[0][:140]

    async def act(self, a: ActorOutput) -> ActionResult:
        res = ActionResult(kind=a.action)
        before = await self.signature()
        mark, t0 = len(self.net), time.time()
        dlg0 = len(self.dialogs)
        loc = self.page.locator(f'[data-sim-id="{a.target}"]').first if a.target is not None else None
        try:
            kind = a.action
            if kind in ("click", "type", "select", "tamper", "repeat") and loc is None:
                raise ValueError(f'"{kind}" needs "target": <element id>. To fill several fields use '
                                 '{"action":"fill","fields":[{"target":ID,"value":"..."}]}')
            if kind == "click":
                res.validation = await self._blocked_submit(loc, enter=False)
                await loc.click(timeout=3500)
            elif kind == "type":
                text = a.value or ""
                submit = text.endswith("\n")
                await self._fill_one(loc, text.rstrip("\n"))
                if submit:
                    res.validation = await self._blocked_submit(loc, enter=True)
                    await loc.press("Enter")
                    res.kind = "click"           # Enter submits: measure the effect like a click
            elif kind == "fill":
                for f in a.fields[:15]:
                    try:
                        await self._fill_one(self.page.locator(f'[data-sim-id="{f["target"]}"]').first, str(f.get("value", "")))
                        res.filled += 1
                    except (PWError, ValueError, KeyError) as e:
                        res.fill_errors.append(f"[{f.get('target')}] {self._human_error(e)}")
                if not res.filled:
                    raise ValueError("no field could be filled: " + "; ".join(res.fill_errors)[:200])
            elif kind == "select":
                try:
                    await loc.select_option(label=a.value, timeout=3000)
                except PWError:
                    await loc.select_option(value=a.value, timeout=3000)
            elif kind == "scroll":
                dy = (self.page.viewport_size or {"height": 700})["height"] * 0.8
                await self.page.mouse.wheel(0, -dy if (a.value or "").lower().startswith("up") else dy)
                await asyncio.sleep(0.4)
            elif kind == "back":
                pages = self.context.pages
                if len(pages) > 1 and await self.page.evaluate("history.length") <= 1:
                    await self.page.close()             # a popup window has no history: going back means closing it
                    self.page = self.context.pages[-1]
                    await self.page.bring_to_front()
                else:
                    await self.page.go_back(wait_until="domcontentloaded", timeout=10000)
            elif kind == "goto":
                await self.page.goto(self.same_site(a.value or "/"), wait_until="domcontentloaded", timeout=20000)
            elif kind == "wait":
                await asyncio.sleep(min(float(a.value or 2), 5))
            elif kind == "fill_form":
                n = await self._eval(FILL_JS)
                if not n:
                    raise ValueError("no empty text fields to fill on this page")
            elif kind == "tamper":
                await loc.evaluate("(el, v) => { el.removeAttribute('readonly'); el.disabled = false; el.value = v;"
                                   " el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }",
                                   a.value or "")
            elif kind == "repeat":
                n = max(2, min(int(a.value or 3) if (a.value or "3").isdigit() else 3, 6))
                await loc.click(timeout=3500)
                for _ in range(n - 1):
                    await asyncio.sleep(0.08)
                    try:
                        await loc.click(timeout=1200, no_wait_after=True)
                    except PWError:
                        break
        except (PWError, ValueError) as e:
            res.ok = False
            res.error = self._human_error(e)
            return await self._finish(res, before, mark, dlg0)

        if res.validation:                   # the user sees the browser's bubble: that IS feedback, no rage-clicking
            await asyncio.sleep(0.5)
        elif res.kind in ("click", "repeat", "goto", "back"):
            res = await self._watch_effect(res, before, t0, a, loc)
        else:
            await self._settle(max_wait=2.0)
        return await self._finish(res, before, mark, dlg0)

    async def _fill_one(self, loc, value: str):
        """Type into a text field, pick a <select> option, or tick a checkbox/radio - whatever the control is."""
        tag, typ = await loc.evaluate("el => [el.tagName.toLowerCase(), (el.type || '').toLowerCase()]")
        if tag == "select":
            try:
                await loc.select_option(label=value, timeout=3000)
            except PWError:
                await loc.select_option(value=value, timeout=3000)
        elif typ in ("checkbox", "radio"):
            if value.strip().lower() in ("false", "no", "off", "uncheck", "0") and typ == "checkbox":
                await loc.uncheck(timeout=3000)
            else:
                await loc.check(timeout=3000)
        else:
            await loc.fill(value, timeout=3500)

    async def _blocked_submit(self, loc, enter: bool) -> list:
        """If this click/Enter submits a form the browser will refuse (required/invalid fields), return the bubbles it shows."""
        try:
            return await loc.evaluate(VALIDATION_JS, enter, timeout=2000) or []
        except PWError:
            return []

    async def _watch_effect(self, res: ActionResult, before: dict, t0: float, a: ActorOutput, loc) -> ActionResult:
        """Wait up to the persona's patience for a visible reaction; impatient personas click again."""
        p = self.persona
        clicks_left = 2 if (p.rage_click and a.action == "click") else 0
        while True:
            deadline = time.time() + p.patience_s
            while time.time() < deadline:
                sig = await self.signature()
                if sig != before:
                    res.changed = True
                    res.first_change_s = round(time.time() - t0, 2)
                    await self._settle(max_wait=6.0)
                    return res
                await asyncio.sleep(0.15)
            res.changed = False
            if clicks_left <= 0:
                return res
            clicks_left -= 1
            res.rage_clicks += 1
            try:
                await loc.click(timeout=1500, no_wait_after=True)
            except PWError:
                return res

    async def _settle(self, max_wait: float = 5.0):
        """Wait for network to go quiet (or the cap), so the next observation sees the final state."""
        end, quiet_since = time.time() + max_wait, None
        while time.time() < end:
            if self.pending == 0:
                quiet_since = quiet_since or time.time()
                if time.time() - quiet_since > 0.35:
                    return
            else:
                quiet_since = None
            await asyncio.sleep(0.1)

    async def _finish(self, res: ActionResult, before: dict, mark: int, dlg0: int) -> ActionResult:
        await self._settle(max_wait=4.0)
        after = await self.signature()
        res.url_changed = after.get("url") != before.get("url")
        res.eventually_changed = after != before
        if res.url_changed and res.validation:   # the form went through after all (e.g. JS-driven submit)
            res.validation = []
        res.dialogs = self.dialogs[dlg0:]
        new = [r for r in self.net[mark:] if not STATIC_EXT.search(r["url"]) and r["type"] in ("xhr", "fetch", "document", "other")]
        res.requests = [f"{r['method']} {urlparse(r['url']).path} {r['status'] or '?'}" for r in new][:8]
        posts = [r for r in new if r["method"] == "POST"]
        counts: dict[str, int] = {}
        for r in posts:
            counts[r["url"]] = counts.get(r["url"], 0) + 1
        res.duplicate_posts = max(counts.values(), default=0)
        return res

    async def close(self):
        try:
            await self.context.close()
        except Exception:  # noqa: BLE001
            pass
