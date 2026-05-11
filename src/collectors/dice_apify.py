"""Dice.com Jobs via Apify (worldunboxer/dice-jobs-scraper).

worldunboxer 的 schema 字段(从社区文档):
    keyword          - 单字符串
    location         - 单字符串
    radius           - 半径数值
    unit             - 半径单位 ("mi" / "km")
    job_entries      - 上限数量
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
    if hours <= 24:
        return "1"
    if hours <= 72:
        return "3"
    if hours <= 168:
        return "7"
    return "30"


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
        title = item.get("title") or item.get("jobTitle") or item.get("position")
        company = (
            item.get("company")
            or item.get("companyName")
            or (item.get("companyInfo") or {}).get("name")
        )
        url = (
            item.get("url")
            or item.get("jobUrl")
            or item.get("detailsUrl")
            or item.get("link")
            or item.get("jobDetailUrl")
        )
        if not (title and company and url):
            return None

        if isinstance(url, str) and url.startswith("/"):
            url = "https://www.dice.com" + url

        return CollectedJob(
            source="dice",
            external_id=str(item.get("id") or item.get("jobId") or url),
            url=url,
            title=str(title).strip(),
            company=str(company).strip(),
            location=item.get("location") or item.get("place"),
            salary=str(item.get("salary") or "") or None,
            description=item.get("description") or item.get("descriptionText"),
            extras={
                "posted": item.get("posted") or item.get("postedDate"),
                "employment_type": item.get("employmentType"),
                "skills": item.get("skills"),
                "work_settings": item.get("workSettings") or item.get("work_settings"),
            },
        )
