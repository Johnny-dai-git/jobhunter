"""Indeed 采集器 - 实装版.

Indeed 不强制登录就能搜索,所以 cookies 可有可无.但加载 cookies 能拿到更多个性化结果.

页面结构 (2025-Q2):
- 搜索结果在 div.job_seen_beacon 或 [data-testid="job-card"]
- 每张卡片: a.jcs-JobTitle, [data-testid="company-name"], [data-testid="text-location"]
- 详情面板在右侧 #jobDescriptionText
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import urlencode

from ._browser import browser_context, polite_wait, scroll_to_bottom
from .base import BaseCollector, CollectedJob


SEARCH_URL = "https://www.indeed.com/jobs"


class IndeedCollector(BaseCollector):
    name = "indeed"

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        seen: set[str] = set()
        count = 0
        with browser_context(self.config, platform="indeed", headless=True) as (page, _, _):
            for kw in keywords:
                for loc in locations:
                    if count >= self.max_per_run:
                        return
                    params = {"q": kw, "l": loc}
                    url = f"{SEARCH_URL}?{urlencode(params)}"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        print(f"[indeed] 打开搜索页失败: {e}")
                        continue
                    polite_wait()

                    # 关闭可能的弹窗
                    for sel in ["button[aria-label='Close']", "#popover-x button"]:
                        try:
                            page.locator(sel).first.click(timeout=1000)
                        except Exception:
                            pass

                    scroll_to_bottom(page, steps=4)

                    cards = page.locator(
                        "div.job_seen_beacon, [data-testid='job-card'], li.css-5lfssm"
                    )
                    n = cards.count()
                    for i in range(n):
                        if count >= self.max_per_run:
                            break
                        c = cards.nth(i)
                        try:
                            link = c.locator("a.jcs-JobTitle, h2 a").first
                            href = link.get_attribute("href") or ""
                            if href.startswith("/"):
                                href = "https://www.indeed.com" + href
                            ext_id = href.split("jk=")[-1].split("&")[0] if "jk=" in href else href
                            if ext_id in seen:
                                continue
                            seen.add(ext_id)

                            title = (link.inner_text() or "").strip()
                            company = (
                                c.locator("[data-testid='company-name'], span.companyName")
                                .first.inner_text()
                            ).strip()
                            location = (
                                c.locator("[data-testid='text-location'], div.companyLocation")
                                .first.inner_text()
                            ).strip()

                            # 点开拿完整 JD
                            try:
                                c.click(timeout=5000)
                                polite_wait(600, 1400)
                                desc_loc = page.locator("#jobDescriptionText")
                                desc = (desc_loc.inner_text() if desc_loc.count() else "").strip()
                            except Exception:
                                desc = ""

                            yield CollectedJob(
                                source="indeed",
                                external_id=ext_id,
                                url=href,
                                title=title,
                                company=company,
                                location=location,
                                description=desc or None,
                            )
                            count += 1
                        except Exception as e:
                            print(f"[indeed] 解析卡片 {i} 失败: {e}")
                            continue
