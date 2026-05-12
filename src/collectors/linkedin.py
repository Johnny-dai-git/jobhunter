"""LinkedIn collector - implementation version.

⚠️ WARNING: LinkedIn terms of service explicitly prohibit automated scraping. Large-scale requests
may result in account restriction or ban. This collector deliberately implements these limits:
1. Must use logged-in cookies (will not use password login)
2. Default to "logged-in user perspective" search URLs, only see what you can see
3. Random delay between requests, limit < 30 items
4. Only first page per keyword/location combination

If you only need a few high-quality jobs for Claude scoring, this controlled usage is manageable.
For large-scale scraping, use LinkedIn's official API.

Search URL:
    https://www.linkedin.com/jobs/search/?keywords={kw}&location={loc}&f_TPR=r{seconds}
- f_TPR=r86400 means last 24 hours (inspired by n8n workflow)
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
                        print(f"[linkedin] failed to open search page: {e}")
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

                            # Details panel
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
                            print(f"[linkedin] failed to parse card {i}: {e}")
                            continue


def _safe_text(locator_root, selector: str) -> str:
    try:
        loc = locator_root.locator(selector).first
        return (loc.inner_text() or "").strip()
    except Exception:
        return ""
