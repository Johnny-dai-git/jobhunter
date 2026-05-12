"""Wellfound (AngelList) Jobs via Apify (clearpath/wellfound-api-ppe).

Wellfound is the go-to startup job board, hub for AI startup hiring.

Possible input schema (by common patterns):
    keyword        - search keyword, often expressed as slug (e.g., 'software-engineer')
    location       - location slug (e.g., 'san-francisco', 'remote')
    maxItems       - max count
    lastDays       - published in last N days
    startUrls      - custom search URL list

Since keyword often needs slug format, auto-convert (Machine Learning Engineer -> machine-learning-engineer).
"""
from __future__ import annotations

from typing import Optional

from .apify_base import ApifyCollector
from .base import CollectedJob


def _slugify(text: str) -> str:
    import re
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


class WellfoundApifyCollector(ApifyCollector):
    name = "wellfound"

    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 24)
        last_days = max(1, max_age_hours // 24)

        # Wellfound usually uses slug format. First keyword is main search term.
        kw_slug = _slugify(keywords[0]) if keywords else ""
        loc_slug = _slugify(locations[0]) if locations else "remote"

        return {
            "keyword": kw_slug,
            "location": loc_slug,
            # Provide actor with multiple field options, actor ignores unrecognized ones
            "keywords": [_slugify(k) for k in keywords],
            "locations": [_slugify(l) for l in locations],
            "maxItems": self.max_per_run,
            "lastDays": last_days,
        }

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        title = item.get("title") or item.get("jobTitle") or item.get("role")
        company = (
            item.get("companyName")
            or item.get("company")
            or (item.get("startup") or {}).get("name")
        )
        url = item.get("url") or item.get("jobUrl") or item.get("link")
        if not (title and company and url):
            return None

        # salary/equity may be nested object
        salary_obj = item.get("salary") or item.get("compensation") or {}
        if isinstance(salary_obj, dict):
            salary = salary_obj.get("text") or salary_obj.get("range")
        else:
            salary = str(salary_obj) if salary_obj else None

        return CollectedJob(
            source="wellfound",
            external_id=str(item.get("id") or url),
            url=url,
            title=str(title).strip(),
            company=str(company).strip(),
            location=item.get("location"),
            salary=salary,
            description=item.get("description") or item.get("descriptionText"),
            extras={
                "equity": item.get("equity"),
                "remote_ok": item.get("remoteOk") or item.get("remote"),
                "company_size": item.get("companySize"),
                "funding": item.get("totalFunding") or item.get("funding"),
            },
        )
