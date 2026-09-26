"""Remote Playwright browser surface; the browser itself runs in Docker elsewhere."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .models import Locator, Target

OBSERVE_SCRIPT = r"""() => {
const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const label = e => e.labels && e.labels.length ? e.labels[0].textContent.trim() : '';
const nodes = [...document.querySelectorAll('input,button,a,select,textarea,h1,h2,h3,td,p,[role]')]
  .filter(visible).slice(0,100);
return {url: location.origin + location.pathname, title: document.title,
  ui_version: document.querySelector('meta[name="bank-ui-version"]')?.content || null,
  controls: nodes.map((e,i) => ({index:i, tag:e.tagName.toLowerCase(),
    text:e.tagName.toLowerCase()==='td' && e.previousElementSibling ? '[value]' :
      (e.textContent || '').trim().slice(0,100), label:label(e).slice(0,80),
    destination:e.formAction || (e.tagName.toLowerCase()==='a' ? e.href : e.form ? e.form.action : null),
    row_label:e.tagName.toLowerCase()==='td' && e.previousElementSibling ?
      e.previousElementSibling.textContent.trim().slice(0,80) : null,
    id:e.id || null, name:e.getAttribute('name'), type:e.getAttribute('type'),
    role:e.getAttribute('role')}))};
}"""


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"


def locator_xpath(locator: Locator) -> str:
    value = xpath_literal(locator.value)
    if locator.strategy == "id":
        return f"//*[@id={value}]"
    if locator.strategy == "name":
        return f"//*[@name={value}]"
    if locator.strategy == "label":
        return f"//label[normalize-space(.)={value}]/following::input[1]"
    if locator.strategy == "text":
        tag = locator.tag or "*"
        if tag not in {"button", "a", "h1", "h2", "h3", "td", "p", "span", "div", "*"}:
            raise ValueError("unsupported text locator tag")
        return f"//{tag}[normalize-space(.)={value}]"
    if locator.strategy == "xpath":
        return locator.value
    raise ValueError("CSS locator has no XPath equivalent")


class DriverError(RuntimeError):
    pass


class BrowserSurface:
    def __init__(self, endpoint: str, check_url: Callable[[str], None] | None = None) -> None:
        self.endpoint = endpoint
        self.check_url = check_url
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self) -> None:
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect(self.endpoint, timeout=10000)
            self.context = await self.browser.new_context(accept_downloads=False)
            if self.check_url is not None:
                async def guard(route: Any) -> None:
                    try:
                        self.check_url(route.request.url)
                    except ValueError:
                        await route.abort()
                    else:
                        await route.continue_()
                await self.context.route("**/*", guard)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(5000)
        except Exception:
            await self.close()
            raise

    async def navigate(self, url: str) -> None:
        assert self.page is not None
        await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)

    async def set_demo_identity(self, url: str, token: str) -> None:
        assert self.context is not None
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        await self.context.add_cookies([{
            "name": "bankgpt_browser", "value": token,
            "domain": parts.hostname, "path": "/demo",
            "httpOnly": True, "secure": parts.scheme == "https", "sameSite": "Strict",
        }])

    async def url(self) -> str:
        assert self.page is not None
        return self.page.url

    async def observe(self) -> dict[str, Any]:
        assert self.page is not None
        return await self.page.evaluate(OBSERVE_SCRIPT)

    async def find(self, target: Target, timeout: float = 5) -> tuple[Any, Locator]:
        assert self.page is not None
        end = asyncio.get_running_loop().time() + timeout
        while True:
            for candidate in target.candidates:
                try:
                    locator = (self.page.locator(candidate.value) if candidate.strategy == "css" else
                               self.page.locator("xpath=" + locator_xpath(candidate)))
                    if await locator.count() == 1 and await locator.is_visible():
                        return locator, candidate
                except Exception:
                    pass
            if asyncio.get_running_loop().time() >= end:
                raise DriverError(f"target not found or ambiguous: {target.description}")
            await asyncio.sleep(0.25)

    async def exists(self, target: Target) -> bool:
        try:
            await self.find(target, timeout=0)
            return True
        except DriverError:
            return False

    async def same_target(self, first: Target, second: Target) -> bool:
        """Check that two locator sets resolve to the same visible DOM element."""
        try:
            left, _ = await self.find(first, timeout=0)
            right, _ = await self.find(second, timeout=0)
            handle = await right.element_handle()
            if handle is None:
                return False
            return await left.evaluate("(element, other) => element === other", handle)
        except DriverError:
            return False

    async def click(self, target: Target) -> Locator:
        locator, chosen = await self.find(target)
        navigates = await locator.evaluate("e => e.tagName === 'A' || !!e.form")
        await locator.click(timeout=5000)
        if navigates:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        return chosen

    async def destination(self, target: Target) -> str | None:
        locator, _ = await self.find(target)
        return await locator.evaluate("e => e.formAction || e.href || (e.form && e.form.action) || null")

    async def control(self, target: Target) -> dict[str, str | None]:
        locator, _ = await self.find(target)
        return await locator.evaluate("e => ({tag:e.tagName.toLowerCase(), text:e.textContent, name:e.getAttribute('name'), "
                                      "label:e.getAttribute('aria-label'), destination:e.formAction || "
                                      "e.href || (e.form && e.form.action) || null})")

    async def type(self, target: Target, value: str) -> Locator:
        locator, chosen = await self.find(target)
        await locator.fill(value, timeout=5000)
        return chosen

    async def text(self, target: Target) -> tuple[str, Locator]:
        locator, chosen = await self.find(target)
        return (await locator.inner_text(timeout=5000)).strip(), chosen

    async def close(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
