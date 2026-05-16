"""Tool ``browser_control`` — минимальный браузер на Playwright.

Только 5 операций: ``goto``, ``search``, ``get_text``, ``screenshot``,
``close``. Никаких AI-кликалок и form-filler-ов: старый код это
поддерживал криво и был источником проблем.

Браузер запускается **lazy** при первом вызове, переиспользуется
между вызовами, закрывается по ``close``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from akli.tools.base import ToolContext, make_spec
from akli.utils.log import get_logger

_log = get_logger("tools.browser")

ACTION_TIMEOUT_MS = 15_000
SCREENSHOT_DIR = Path.home() / "Pictures" / "Akli"


class _Browser:
    """Долгоживущий Playwright-инстанс."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._page = None

    async def _ensure(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=False)
        ctx = await self._browser.new_context()
        self._page = await ctx.new_page()
        self._page.set_default_timeout(ACTION_TIMEOUT_MS)
        return self._page

    async def goto(self, url: str) -> str:
        async with self._lock:
            page = await self._ensure()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            await page.goto(url, wait_until="domcontentloaded")
            return f"Opened {url}"

    async def search(self, query: str) -> str:
        return await self.goto(f"https://duckduckgo.com/?q={quote_plus(query)}")

    async def get_text(self) -> str:
        async with self._lock:
            if self._page is None:
                return "Browser is not open."
            text = await self._page.evaluate("() => document.body.innerText")
            text = " ".join(text.split())
            return text[:4000] + ("…" if len(text) > 4000 else "")

    async def screenshot(self) -> str:
        async with self._lock:
            if self._page is None:
                return "Browser is not open."
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = SCREENSHOT_DIR / f"akli_{int(time.time())}.png"
            await self._page.screenshot(path=str(path), full_page=False)
            return f"Saved screenshot to {path}"

    async def close(self) -> str:
        async with self._lock:
            if self._page is None:
                return "Browser already closed."
            try:
                await self._page.context.close()
            except Exception:
                pass
            try:
                await self._browser.close()
            except Exception:
                pass
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._page = None
            self._browser = None
            self._playwright = None
            return "Browser closed."


_singleton: Optional[_Browser] = None


def _get_browser() -> _Browser:
    global _singleton
    if _singleton is None:
        _singleton = _Browser()
    return _singleton


async def _run(params: dict, ctx: ToolContext) -> str:
    action = (params.get("action") or "").strip().lower()
    browser = _get_browser()

    if action in ("goto", "open"):
        url = (params.get("url") or "").strip()
        if not url:
            return "I need a URL."
        return await asyncio.wait_for(browser.goto(url), timeout=20)
    if action == "search":
        query = (params.get("query") or params.get("url") or "").strip()
        if not query:
            return "I need a search query."
        return await asyncio.wait_for(browser.search(query), timeout=20)
    if action in ("get_text", "extract", "read"):
        return await asyncio.wait_for(browser.get_text(), timeout=20)
    if action == "screenshot":
        return await asyncio.wait_for(browser.screenshot(), timeout=20)
    if action == "close":
        return await asyncio.wait_for(browser.close(), timeout=10)

    return f"Unknown action '{action}'. Use goto/search/get_text/screenshot/close."


SPEC = make_spec(
    name        = "browser_control",
    description = (
        "Headed browser. Actions: goto (open URL), search (DuckDuckGo), "
        "get_text (extract current page text), screenshot, close."
    ),
    parameters  = {
        "action": {"type": "string", "description": "goto | search | get_text | screenshot | close"},
        "url":    {"type": "string", "description": "URL for 'goto'."},
        "query":  {"type": "string", "description": "Query for 'search'."},
    },
    required = ["action"],
    run      = _run,
)
