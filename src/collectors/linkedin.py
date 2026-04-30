"""LinkedIn 采集器 - 实装版.

⚠️ 警告: LinkedIn 用户协议明令禁止自动化抓取. 大量请求可能导致账号被限或封禁.
本采集器特意做了以下限制:
1. 必须使用你登录后的 cookies (不会用密码登录)
2. 默认走"已登录用户视角"的搜索 URL,只看你能看的
3. 每次请求间随机延迟,limit < 30 个
4. 每个 keyword/location 组合只翻第一页

如果你只是要找几个高质量岗位让 Claude 评分,这种节制的用法风险可控.
要做大规模爬取,请用 LinkedIn 官方 API.

搜索 URL:
    https://www.linkedin.com/jobs/search/?keywords={kw}&location={loc}&f_TPR=r{seconds}
- f_TPR=r86400 表示最近 24 小时 (借鉴 n8n 工作流)
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import urlencode

from ._browser import browser_context, polite_wait, scroll_to_bottom
from .base import BaseCollector, CollectedJob


SEARCH_URL = "https://www.linkedin.com/jobs/search/"


class LinkedInCollector(BaseCollector):
    name = "linkedin"

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 0)
        f_tpr = f"r{max_age_hours * 3600}" if max_age_hours else None

        seen: set[str] = set()
        count = 0
        with browser_context(self.config, platform="linkedin", headless=True) as (page, _, _):
            for kw in keywords:
                for loc in locations:
                    if count >= self.max_per_run:
                        return
                    params = {"keywords": kw, "location": loc}
                    if f_tpr:
                        params["f_TPR"] = f_tpr
                    url = f"{SEARCH_URL}?{urlencode(params)}"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        print(f"[linkedin] 打开搜索页失败: {e}")
                        continue
                    polite_wait(1500, 3000)
                    scroll_to_bottom(page, steps=4, pause_ms=900)

                    cards = page.locator(
                        "div.job-card-container, li.jobs-search-results__list-item, "
                        "div.base-card[data-entity-urn*='jobPosting']"
                    )
                    n = cards.count()
                    for i in range(n):
                        if count >= self.max_per_run:
                            break
                        c = cards.nth(i)
                        try:
                            link = c.locator("a.job-card-list__title, a.base-card__full-link").first
                            href = link.get_attribute("href") or ""
                            if href.startswith("/"):
                                href = "https://www.linkedin.com" + href
                            ext_id = href.split("?")[0].rstrip("/").split("/")[-1]
                            if ext_id in seen:
                                continue
                            seen.add(ext_id)

                            title = (link.inner_text() or "").strip()
                            company = _safe_text(
                                c,
                                "a.job-card-container__company-name, "
                                "h4.base-search-card__subtitle, "
                                ".job-card-container__primary-description"
                            )
                            location = _safe_text(
                                c,
                                "li.job-card-container__metadata-item, "
                                ".job-search-card__location"
                            )

                            # 详情面板
                            desc = ""
                            try:
                                c.click(timeout=5000)
                                polite_wait(900, 1800)
                                desc_loc = page.locator(
                                    "div.jobs-description__content, "
                                    "div.jobs-description-content__text, "
                                    "div#job-details"
                                )
                                if desc_loc.count():
                                    desc = desc_loc.first.inner_text().strip()
                            except Exception:
                                pass

                            yield CollectedJob(
                                source="linkedin",
                                external_id=ext_id,
                                url=href,
                                title=title,
                                company=company,
                                location=location,
                                description=desc or None,
                            )
                            count += 1
                        except Exception as e:
                            print(f"[linkedin] 解析卡片 {i} 失败: {e}")
                            continue


def _safe_text(locator_root, selector: str) -> str:
    try:
        loc = locator_root.locator(selector).first
        return (loc.inner_text() or "").strip()
    except Exception:
        return ""
