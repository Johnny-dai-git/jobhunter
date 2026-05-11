"""Wellfound (AngelList) Jobs via Apify (clearpath/wellfound-api-ppe).

Wellfound 是创业公司岗位库的标杆,AI startup 招聘集中地.

可能的 input schema (按常见模式):
    keyword        - 搜索关键词,常被表达为 slug (例: 'software-engineer')
    location       - 地点 slug (例: 'san-francisco', 'remote')
    maxItems       - 最大数量
    lastDays       - 最近 N 天发布
    startUrls      - 自定义搜索 URL 列表

由于 keyword 经常需要 slug 格式,自动转换 (Machine Learning Engineer -> machine-learning-engineer).
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

        # Wellfound 通常用 slug 形式. 第一个 keyword 当主搜索词.
        kw_slug = _slugify(keywords[0]) if keywords else ""
        loc_slug = _slugify(locations[0]) if locations else "remote"

        return {
            "keyword": kw_slug,
            "location": loc_slug,
            # 给 actor 多种字段尝试,actor 会忽略它不认识的
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

        # salary/equity 可能是嵌套对象
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
