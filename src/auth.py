"""登录助手: 打开浏览器让你手动登录,然后保存 cookies 供后续无人值守使用.

为什么要手动登录?
- 大平台都有反自动化检测.脚本登录极易触发风控甚至封号.
- 手动登录最安全,且只需要做一次(cookies 失效后再来一次).
- 我们绝不存你的密码.
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

# 登录后访问这个 URL 验证 cookies 是否有效
PLATFORM_VERIFY_URLS = {
    "linkedin": "https://www.linkedin.com/feed/",
    "indeed": "https://www.indeed.com/",
    "glassdoor": "https://www.glassdoor.com/member/home/index.htm",
    "ziprecruiter": "https://www.ziprecruiter.com/candidate/dashboard",
}


def cookie_path(config: Config, platform: str) -> Path:
    """统一从 config.yaml 中拿 cookie 文件路径,默认放 data/cookies/."""
    platform = platform.lower()
    settings = config.collectors.get(platform, {})
    rel = settings.get("cookie_file") or f"data/cookies/{platform}.json"
    p = (config.project_root / rel).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def login_and_save(config: Config, platform: str, timeout_sec: int = 300) -> Path:
    """打开 Chromium,导航到登录页,等你登录完之后保存 cookies."""
    from playwright.sync_api import sync_playwright

    platform = platform.lower()
    if platform not in PLATFORM_LOGIN_URLS:
        raise ValueError(f"未知平台: {platform}. 支持: {list(PLATFORM_LOGIN_URLS)}")

    url = PLATFORM_LOGIN_URLS[platform]
    save_path = cookie_path(config, platform)

    print(f"\n→ 即将打开浏览器,请在 {timeout_sec} 秒内完成 {platform} 登录")
    print(f"  登录后我会自动保存 cookies 到: {save_path}")
    print(f"  ⚠️  请勾选 '记住我' / 'Remember me'")

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
        print(f"\n请登录.登录后我会每 3 秒检测一次 {verify_url} 是否能访问...")
        deadline = time.time() + timeout_sec
        logged_in = False
        while time.time() < deadline:
            try:
                # 在新标签里偷偷验证,不打扰你的登录页
                check_page = ctx.new_page()
                check_page.goto(verify_url, wait_until="domcontentloaded", timeout=10000)
                # 简单判断: 如果 URL 不是登录页 且 页面里没有 "sign in" 之类
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
            raise TimeoutError("登录超时,请重试 (或加大 --timeout)")

        cookies = ctx.cookies()
        save_path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False))
        print(f"\n✓ 已保存 {len(cookies)} 个 cookie 到 {save_path}")
        browser.close()

    return save_path


def load_cookies(config: Config, platform: str) -> list[dict]:
    """读取已保存的 cookies."""
    p = cookie_path(config, platform)
    if not p.exists():
        raise FileNotFoundError(
            f"{platform} 还没登录过.先跑: python3 -m src.main login --platform {platform}"
        )
    return json.loads(p.read_text())
