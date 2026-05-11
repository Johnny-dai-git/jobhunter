"""LinkedIn 通过 Apify (默认用 bebity/linkedin-jobs-scraper).

这个 Actor 的输入 schema (常见字段):
    title          - 搜索关键词,字符串
    location       - 地点字符串
    rows           - 想要的岗位数 (近似)
    publishedAt    - r24 / r604800 / r2592000 (24h / 7d / 30d)

输出每条 item 大致:
    {
        "id": "...",
        "title": "Senior ML Infrastructure Engineer",
        "companyName": "Anthropic",
        "location": "San Francisco, CA",
        "description": "...",
        "applyUrl": "https://...",
        "link": "https://linkedin.com/jobs/view/...",
        "salary": "$200K - $300K",
        "publishedAt": "2026-05-10T...",
        ...
    }

如果你换用别家的 Actor (比如 curious_coder/linkedin-job-finder),只要按它的
schema 改写 _build_input / _parse_item 即可,基类不用动.
"""
from __future__ import annotations

from typing import Optional

from .apify_base import ApifyCollector
from .base import CollectedJob


class LinkedInApifyCollector(ApifyCollector):
    name = "linkedin"

    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        # 把 max_age_hours 翻译成 bebity Actor 的 publishedAt 参数
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 0)
        if max_age_hours <= 24:
            published_at = "r86400"     # 24h
        elif max_age_hours <= 24 * 7:
            published_at = "r604800"    # 7 days
        else:
            published_at = "r2592000"   # 30 days

        return {
            # bebity 的 schema 支持多 query 数组,我们生成 keyword × location
            "queries": [
                {
                    "title": kw,
                    "location": loc,
                    "publishedAt": published_at,
                    "rows": max(1, self.max_per_run // max(1, len(keywords) * len(locations))),
                }
                for kw in keywords
                for loc in locations
            ],
            # 退路: 一些版本的 actor 用单数参数
            "title": keywords[0] if keywords else "",
            "location": locations[0] if locations else "",
            "rows": self.max_per_run,
            "publishedAt": published_at,
        }

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        # 不同 actor 字段名稍有差异,做一些 fallback
        title = item.get("title") or item.get("jobTitle")
        company = (
            item.get("companyName")
            or item.get("company")
            or item.get("companyTitle")
        )
        url = (
            item.get("link")
            or item.get("jobUrl")
            or item.get("url")
            or item.get("applyUrl")
        )
        if not (title and company and url):
            return None

        return CollectedJob(
            source="linkedin",
            external_id=str(item.get("id") or item.get("jobId") or url),
            url=url,
            title=str(title).strip(),
            company=str(company).strip(),
            location=item.get("location") or item.get("locationName"),
            salary=item.get("salary") or item.get("salaryInfo"),
            description=item.get("description") or item.get("descriptionText"),
            extras={
                "published_at": item.get("publishedAt") or item.get("postedAt"),
                "apply_url": item.get("applyUrl"),
                "employment_type": item.get("employmentType"),
            },
        )
