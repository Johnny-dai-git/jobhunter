"""Y Combinator Work at a Startup via Apify (artemlazarevm/yc-jobs-scraper).

artemlazarevm 是 YC jobs scraper 里口碑较好的一个,scrapes workatastartup.com.

可能的 input schema (按常见模式,actor 实际值可能略有差异):
    keyword       - 搜索关键词
    location      - 地点
    maxItems      - 上限
    batch         - YC batch 过滤 (例如 'W24', 'S24')
    industry      - 行业过滤

如果 actor 拒绝某些字段,看 console 报错并改 input_overrides.

输出常见字段:
    company / companyName
    title / role
    url / jobUrl
    location
    description
    salary / equity
    batch  (例如 'W23')
"""
from __future__ import annotations

from typing import Optional

from .apify_base import ApifyCollector
from .base import CollectedJob


class YCApifyCollector(ApifyCollector):
    name = "yc"

    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        # YC scraper 一般支持单 keyword + 单 location 或 startUrls
        # 我们 fallback 用第一个 keyword + 第一个 location
        return {
            "keyword": keywords[0] if keywords else "",
            "location": locations[0] if locations else "",
            "keywords": keywords,        # 有些 actor 支持数组
            "locations": locations,
            "maxItems": self.max_per_run,
        }

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        # 兼容各种字段名变体
        title = item.get("title") or item.get("role") or item.get("jobTitle")
        company = (
            item.get("companyName")
            or item.get("company")
            or (item.get("startup") or {}).get("name")
        )
        url = (
            item.get("url")
            or item.get("jobUrl")
            or item.get("link")
            or item.get("applyUrl")
        )
        if not (title and company and url):
            return None

        location = item.get("location") or item.get("city")
        if isinstance(location, list):
            location = ", ".join(location)

        salary_or_equity = item.get("salary") or item.get("compensation") or item.get("equity")
        if isinstance(salary_or_equity, dict):
            salary_or_equity = salary_or_equity.get("text")

        return CollectedJob(
            source="yc",
            external_id=str(item.get("id") or item.get("jobId") or url),
            url=url,
            title=str(title).strip(),
            company=str(company).strip(),
            location=str(location).strip() if location else None,
            salary=str(salary_or_equity) if salary_or_equity else None,
            description=item.get("description") or item.get("descriptionText"),
            extras={
                "batch": item.get("batch") or item.get("ycBatch"),
                "industry": item.get("industry"),
                "team_size": item.get("teamSize") or item.get("employeeCount"),
                "equity": item.get("equity"),
            },
        )
