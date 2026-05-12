"""Dice.com Jobs via Apify (worldunboxer/dice-jobs-scraper).

worldunboxer schema fields (from community documentation):
    keyword          - single string
    location         - single string
    radius           - radius value
    unit             - radius unit ("mi" / "km")
    job_entries      - max count
    posted_date      - "Today" / "1" / "3" / "7" / "30"
    employment_type  - ["FULLTIME", "PARTTIME", "CONTRACTS"]
    employer_type    - ["Direct Hire", "Recruiter"]
    work_settings    - ["On-Site", "Hybrid", "Remote"]
    easy_apply       - bool
    willing_to_sponsor - bool
"""
from __future__ import annotations

from typing import Optional

from .apify_base import ApifyCollector
from .base import CollectedJob


def _hours_to_posted(hours: int) -> str:
    """worldunboxer uses enum strings: ANY / ONE / THREE / SEVEN"""
    if hours <= 24:
        return "ONE"
    if hours <= 72:
        return "THREE"
    if hours <= 168:
        return "SEVEN"
    return "ANY"


class DiceApifyCollector(ApifyCollector):
    name = "dice"

    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 24)
        return {
            "keyword": keywords[0] if keywords else "",
            "location": locations[0] if locations else "United States",
            "radius": 30,
            "unit": "mi",
            "job_entries": self.max_per_run,
            "posted_date": _hours_to_posted(max_age_hours),
            "employment_type": ["FULLTIME"],
        }

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        """worldunboxer output fields confirmed (real schema):
        title / company / details_page_url / job_id / summary /
        location / salary / employment_type / posted_date / is_remote / ...
        """
        title = item.get("title") or item.get("jobTitle")
        company = item.get("company") or item.get("companyName")
        url = (
            item.get("details_page_url")
            or item.get("url")
            or item.get("jobUrl")
            or item.get("link")
        )
        if not (title and company and url):
            return None

        if isinstance(url, str) and url.startswith("/"):
            url = "https://www.dice.com" + url

        return CollectedJob(
            source="dice",
            external_id=str(item.get("job_id") or item.get("guid") or url),
            url=url,
            title=str(title).strip(),
            company=str(company).strip(),
            location=item.get("location"),
            salary=str(item.get("salary") or "").strip() or None,
            description=item.get("summary") or item.get("description"),
            extras={
                "posted_date": item.get("posted_date"),
                "employment_type": item.get("employment_type"),
                "is_remote": item.get("is_remote"),
                "willing_to_sponsor": item.get("willing_to_sponsor"),
                "easy_apply": item.get("easy_apply"),
            },
        )
