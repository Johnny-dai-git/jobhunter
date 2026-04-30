"""Glassdoor 采集器 - 实装版.

Glassdoor 反爬较强,需要登录 cookies. 搜索 URL:
    https://www.glassdoor.com/Job/jobs.htm?sc.keyword={kw}&locKeyword={loc}&fromAge={hours}
- fromAge: 1=24h, 3=3天, 7=一周
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import urlencode

from ._browser import browser_context, polite_wait, scroll_to_bottom
from .base import BaseCollector, CollectedJob


SEARCH_URL = "https://www.glassdoor.com/Job/jobs.htm"


class GlassdoorCollector(BaseCollector):
    name = "glassdoor"

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 0)
        from_age_days = max(1, max_age_hours // 24) if max_age_hours else 0

        seen: set[str] = set()
        count = 0
        with browser_context(self.config, platform="glassdoor", headless=True) as (page, _, _):
            for kw in keywords:
                for loc in locations:
                    if count >= self.max_per_run:
                        return
                    params = {"sc.keyword": kw, "locKeyword": loc}
                    if from_age_days:
                        params["fromAge"] = str(from_age_days)
                    url = f"{SEARCH_URL}?{urlencode(params)}"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        print(f"[glassdoor] 打开搜索页失败: {e}")
                        continue
                    polite_wait()

                    # 关掉常见弹窗
                    for sel in [
                        "button.modal_closeIcon",
                        "button[aria-label='Close']",
                        "[data-test='modal-close']",
                    ]:
                        try:
                            page.locator(sel).first.click(timeout=1000)
                        except Exception:
                            pass

                    scroll_to_bottom(page, steps=5)

                    cards = page.locator(
                        "li[data-test='jobListing'], div.JobsList_jobListItem"
                    )
                    n = cards.count()
                    for i in range(n):
                        if count >= self.max_per_run:
                            break
                        c = cards.nth(i)
                        try:
                            link = c.locator("a[data-test='job-link'], a.JobCard_jobTitle").first
                            href = link.get_attribute("href") or ""
                            if href.startswith("/"):
                                href = "https://www.glassdoor.com" + href
                            ext_id = href.split("?")[0].rstrip("/").split("/")[-1]
                            if ext_id in seen:
                                continue
                            seen.add(ext_id)

                            title = (link.inner_text() or "").strip()
                            company = _safe_text(c, "[data-test='employer-name'], .EmployerProfile_compactEmployerName")
                            location = _safe_text(c, "[data-test='emp-location'], .JobCard_location")
                            salary = _safe_text(c, "[data-test='detailSalary'], .JobCard_salaryEstimate")

                            # 点开详情拿 JD
                            desc = ""
                            try:
                                c.click(timeout=5000)
                                polite_wait(700, 1500)
                                desc_loc = page.locator(
                                    "[data-test='jobDescriptionContainer'], .JobDetails_jobDescription"
                                )
                                if desc_loc.count():
                                    desc = desc_loc.first.inner_text().strip()
                            except Exception:
                                pass

                            yield CollectedJob(
                                source="glassdoor",
                                external_id=ext_id,
                                url=href,
                                title=title,
                                company=company,
                                location=location,
                                salary=salary or None,
                                description=desc or None,
                            )
                            count += 1
                        except Exception as e:
                            print(f"[glassdoor] 解析卡片 {i} 失败: {e}")
                            continue


def _safe_text(locator_root, selector: str) -> str:
    try:
        loc = locator_root.locator(selector).first
        return (loc.inner_text() or "").strip()
    except Exception:
        return ""
