"""Playwright 共用工具: 启动浏览器、加载 cookies、温柔地滚屏、节流."""
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
    """启动 Chromium,可选加载某平台的 cookies. 用 with 语法保证关闭.

    yield (page, browser, ctx) 三元组.
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
                print(f"[警告] {e}")

        page = ctx.new_page()
        try:
            yield page, browser, ctx
        finally:
            browser.close()


def polite_wait(min_ms: int = 800, max_ms: int = 2200) -> None:
    """随机延迟,降低被检测概率."""
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def scroll_to_bottom(page, *, steps: int = 6, pause_ms: int = 600) -> None:
    """逐步往下滚,触发懒加载."""
    for _ in range(steps):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(pause_ms)
