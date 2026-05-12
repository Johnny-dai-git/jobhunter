"""ZipRecruiter collector - implementation version.

Search URL: https://www.ziprecruiter.com/jobs-search?search={kw}&location={loc}&days=1
- days=1 means only jobs from last 24 hours (inspired by n8n workflow's "24h filter" idea)
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import urlencode

from ._browser import browser_context, polite_wait, scroll_to_bottom
from .base import BaseCollector, CollectedJob


SEARCH_URL = "https://www.ziprecruiter.com/jobs-search"


class ZipRecruiterCollector(BaseCollector):
    name = "ziprecruiter"

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 0)
        # ZipRecruiter's days parameter: 1=24h, 5=past 5 days
        days = max(1, max_age_hours // 24) if max_age_hours else 0

        seen: set[str] = set()
        count = 0
        with browser_context(self.config, platform="ziprecruiter", headless=True) as (page, _, _):
            for kw in keywords:
                for loc in locations:
                    if count >= self.max_per_run:
                        return
                    params = {"search": kw, "location": loc}
                    if days:
                        params["days"] = str(days)
                    url = f"{SEARCH_URL}?{urlencode(params)}"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        print(f"[ziprecruiter] failed to open search page: {e}")
                        continue
                    polite_wait()
                    scroll_to_bottom(page, steps=5)

                    cards = page.locator(
                        "article.job_result, [data-testid='job-card'], div.job_content"
                    )
                    n = cards.count()
                    for i in range(n):
                        if count >= self.max_per_run:
                            break
                        c = cards.nth(i)
                        try:
                            link = c.locator("a.job_link, h2 a").first
                            href = link.get_attribute("href") or ""
                            if href.startswith("/"):
                                href = "https://www.ziprecruiter.com" + href
                            ext_id = href.split("?")[0].rstrip("/").split("/")[-1]
                            if ext_id in seen:
                                continue
                            seen.add(ext_id)

                            title = (link.inner_text() or "").strip()
                            company = _safe_text(c, "a.t_org_link, [class*='org_name']")
                            location = _safe_text(c, "[class*='location'], a.t_location_link")
                            snippet = _safe_text(c, ".job_snippet, [class*='snippet']")

                            # Get complete job description from detail page
                            desc = snippet
                            try:
                                detail = page.context.new_page()
                                detail.goto(href, wait_until="domcontentloaded", timeout=20000)
                                polite_wait(500, 1100)
                                desc_loc = detail.locator(
                                    "[data-testid='job-description'], .job_description, #job-description"
                                )
                                if desc_loc.count():
                                    desc = desc_loc.first.inner_text().strip()
                                detail.close()
                            except Exception:
                                pass

                            yield CollectedJob(
                                source="ziprecruiter",
                                external_id=ext_id,
                                url=href,
                                title=title,
                                company=company,
                                location=location,
                                description=desc or None,
                            )
                            count += 1
                        except Exception as e:
                            print(f"[ziprecruiter] failed to parse card {i}: {e}")
                            continue


def _safe_text(locator_root, selector: str) -> str:
    try:
        loc = locator_root.locator(selector).first
        return (loc.inner_text() or "").strip()
    except Exception:
        return ""
