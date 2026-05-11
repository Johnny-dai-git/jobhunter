"""Dice.com Jobs via Apify (scrapestorm/dice-jobs-scraper-fast-cheap).

Dice 是美国传统科技岗位库,云/SRE/DevOps/工程师岗位密度高.

Input schema 示例:
    {
        "keyword": "Software development",
        "place": "New York",
        "published": "Last 3 days",  # 枚举: Today/Last 24 hours/Last 3 days/Last 7 days
        "rayon": "5",                # 半径,英里
        "maxitems": 250
    }
"""
from __future__ import annotations

from typing import Optional

from .apify_base import ApifyCollector
from .base import CollectedJob


def _hours_to_published(hours: int) -> str:
    if hours <= 24:
        return "Last 24 hours"
    if hours <= 72:
        return "Last 3 days"
    return "Last 7 days"


class DiceApifyCollector(ApifyCollector):
    name = "dice"

    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 24)
        # Dice 大概率支持单 keyword + 单 place
        return {
            "keyword": keywords[0] if keywords else "",
            "place": locations[0] if locations else "United States",
            "published": _hours_to_published(max_age_hours),
            "rayon": "30",
            "maxitems": self.max_per_run,
            # 给个备选数组,有的 actor 支持
            "keywords": keywords,
            "places": locations,
        }

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        title = item.get("title") or item.get("jobTitle") or item.get("position")
        company = (
            item.get("company")
            or item.get("companyName")
            or (item.get("companyInfo") or {}).get("name")
        )
        url = item.get("url") or item.get("jobUrl") or item.get("detailsUrl") or item.get("link")
        if not (title and company and url):
            return None

        # 拼接 URL 如果是相对路径
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
            },
        )
