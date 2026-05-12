"""Playwright shared utilities: launch browser, load cookies, gentle scrolling, rate limiting."""
from __future__ import annotations

import random
import time
from contextlib import contextmanager
from typing import Iterator

from ..auth import load_cookies
from ..config import Config


DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@contextmanager
def browser_context(
    config: Config,
    platform: str | None = None,
    *,
    headless: bool = True,
) -> Iterator:
    """Launch Chromium, optionally load cookies for a platform. Use with syntax to ensure cleanup.

    Yields (page, browser, ctx) tuple.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=30)
        ctx = browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1280, "height": 900})

        if platform:
            try:
                cookies = load_cookies(config, platform)
                ctx.add_cookies(cookies)
            except FileNotFoundError as e:
                print(f"[warning] {e}")

        page = ctx.new_page()
        try:
            yield page, browser, ctx
        finally:
            browser.close()


def polite_wait(min_ms: int = 800, max_ms: int = 2200) -> None:
    """Random delay to reduce detection probability."""
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def scroll_to_bottom(page, *, steps: int = 6, pause_ms: int = 600) -> None:
    """Scroll gradually to trigger lazy loading."""
    for _ in range(steps):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(pause_ms)
