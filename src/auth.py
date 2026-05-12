"""Login helper: opens browser for manual login, then saves cookies for subsequent unattended use.

Why manual login?
- Large platforms have anti-automation detection. Script-based login easily triggers risk controls or account bans.
- Manual login is safest and only needs to be done once (repeat if cookies expire).
- We never store your password.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config


PLATFORM_LOGIN_URLS = {
    "linkedin": "https://www.linkedin.com/login",
    "indeed": "https://secure.indeed.com/auth",
    "glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
    "ziprecruiter": "https://www.ziprecruiter.com/authn/login",
}

# Access this URL after login to verify cookies are valid
PLATFORM_VERIFY_URLS = {
    "linkedin": "https://www.linkedin.com/feed/",
    "indeed": "https://www.indeed.com/",
    "glassdoor": "https://www.glassdoor.com/member/home/index.htm",
    "ziprecruiter": "https://www.ziprecruiter.com/candidate/dashboard",
}


def cookie_path(config: Config, platform: str) -> Path:
    """Unified cookie file path from config.yaml, default location data/cookies/."""
    platform = platform.lower()
    settings = config.collectors.get(platform, {})
    rel = settings.get("cookie_file") or f"data/cookies/{platform}.json"
    p = (config.project_root / rel).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def login_and_save(config: Config, platform: str, timeout_sec: int = 300) -> Path:
    """Open Chromium, navigate to login page, save cookies after you log in."""
    from playwright.sync_api import sync_playwright

    platform = platform.lower()
    if platform not in PLATFORM_LOGIN_URLS:
        raise ValueError(f"Unknown platform: {platform}. Supported: {list(PLATFORM_LOGIN_URLS)}")

    url = PLATFORM_LOGIN_URLS[platform]
    save_path = cookie_path(config, platform)

    print(f"\n→ About to open browser, please complete {platform} login within {timeout_sec} seconds")
    print(f"  I will automatically save cookies to: {save_path}")
    print(f"  ⚠️  Please check 'Remember me'")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        page.goto(url)

        verify_url = PLATFORM_VERIFY_URLS.get(platform, url)
        print(f"\nPlease log in. I will check every 3 seconds if {verify_url} is accessible...")
        deadline = time.time() + timeout_sec
        logged_in = False
        while time.time() < deadline:
            try:
                # Silently verify in new tab, not disturbing your login page
                check_page = ctx.new_page()
                check_page.goto(verify_url, wait_until="domcontentloaded", timeout=10000)
                # Simple check: if URL is not login page and page doesn't contain "sign in"
                final = check_page.url.lower()
                content = check_page.content().lower()
                if "login" not in final and "/authn" not in final and "sign in" not in content[:5000]:
                    logged_in = True
                check_page.close()
                if logged_in:
                    break
            except Exception:
                pass
            time.sleep(3)

        if not logged_in:
            browser.close()
            raise TimeoutError("Login timeout, please retry (or increase --timeout)")

        cookies = ctx.cookies()
        save_path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False))
        print(f"\n✓ Saved {len(cookies)} cookies to {save_path}")
        browser.close()

    return save_path


def load_cookies(config: Config, platform: str) -> list[dict]:
    """Load saved cookies."""
    p = cookie_path(config, platform)
    if not p.exists():
        raise FileNotFoundError(
            f"{platform} not logged in yet. First run: python3 -m src.main login --platform {platform}"
        )
    return json.loads(p.read_text())
