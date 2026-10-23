"""Playwright session for one persona: device/network emulation, observation, actions, effect measurement."""
import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Error as PWError, Page

from backend.models import ActorOutput, Persona
from backend.navigator.dom import AUDIT_JS, EXTRACT_JS, FILL_JS, SIGNATURE_JS, SOM_OFF_JS, SOM_ON_JS

# "slow 3G"-ish: 400ms RTT, ~1.2 Mbit/s down, ~600 kbit/s up
SLOW_3G = dict(offline=False, latency=400, downloadThroughput=150 * 1024, uploadThroughput=75 * 1024)
STATIC_EXT = re.compile(r"\.(css|js|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|map)(\?|$)", re.I)


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

    def summary(self) -> str:
        if not self.ok:
            return f"FAILED: {self.error}"
        if self.kind in ("type", "select", "tamper", "fill_form"):
            return "value entered locally - NOTHING was sent to the server; click the page's submit/Update/Apply/Pay button to send it"
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
        """Stay on the site under test: block top-level navigation to other origins."""
        req = route.request
        if req.is_navigation_request() and req.url.startswith("http") and urlparse(req.url).netloc != self.origin:
            await route.abort()
        else:
            await route.continue_()

    def _on_new_page(self, page: Page):
        self._attach(page)
        self.page = page

    def _attach(self, page: Page):
        self.page = page
        page.on("console", lambda m: self.console_errors.append(m.text[:200]) if m.type == "error" else None)
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
        if urlparse(full).netloc != self.origin:
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
        return await self.page.screenshot(type="jpeg", quality=quality, timeout=15000)

    async def som_screenshot(self) -> bytes:
        await self._eval(SOM_ON_JS)
        try:
            return await self.screenshot(quality=70)
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
        if "not allowed" in m:
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
                raise ValueError("this action needs a target element id")
            if kind == "click":
                await loc.click(timeout=3500)
            elif kind == "type":
                text = a.value or ""
                submit = text.endswith("\n")
                await loc.fill(text.rstrip("\n"), timeout=3500)
                if submit:
                    await loc.press("Enter")
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

        if a.action in ("click", "repeat", "goto", "back"):
            res = await self._watch_effect(res, before, t0, a, loc)
        else:
            await self._settle(max_wait=2.0)
        return await self._finish(res, before, mark, dlg0)

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
