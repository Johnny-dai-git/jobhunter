"""Indeed 通过 Apify (misceres/indeed-scraper).

Actor schema (https://apify.com/misceres/indeed-scraper/input-schema):
    position           - single string (单关键词)
    location           - single string
    country            - enum,默认 'US'
    maxItemsPerSearch  - 每个搜索的上限
    parseCompanyDetails - 是否抓公司详情
    saveOnlyUniqueItems - 自动去重
    startUrls          - array of URLs,支持多个搜索 URL

我们用 startUrls 方式: 生成 N 个搜索 URL (keyword × location 组合),
一次 actor call 完成所有搜索. 价格 ~$0.005/job.

输出每条 (常见字段, 用 fallback 兼容):
    {
        "positionName": "...",     或 "position", "title"
        "company": "...",
        "location": "...",
        "description": "...",
        "salary": "...",
        "url": "...",              详情页 URL
        "jobKey": "..."
    }
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from .apify_base import ApifyCollector
from .base import CollectedJob


def _hours_to_fromage(hours: int) -> str:
    """Indeed 用 fromage=1/3/7/14 表示过去 1/3/7/14 天."""
    if hours <= 24:
        return "1"
    if hours <= 24 * 3:
        return "3"
    if hours <= 24 * 7:
        return "7"
    return "14"


class IndeedApifyCollector(ApifyCollector):
    name = "indeed"

    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 24)
        fromage = _hours_to_fromage(max_age_hours)

        # 生成 N 个搜索 URL,每个一对 (keyword, location)
        start_urls = []
        for kw in keywords:
            for loc in locations:
                params = {"q": kw, "l": loc, "fromage": fromage}
                start_urls.append({
                    "url": f"https://www.indeed.com/jobs?{urlencode(params)}"
                })

        # 总量除以 search 数, 取上限
        per_search = max(1, self.max_per_run // max(1, len(start_urls)))

        return {
            "startUrls": start_urls,
            "maxItemsPerSearch": per_search,
            "country": "US",
            "parseCompanyDetails": False,    # 省钱: 不抓公司详情
            "saveOnlyUniqueItems": True,
        }

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        # misceres 输出字段不固定,做 fallback
        title = (
            item.get("positionName")
            or item.get("position")
            or item.get("title")
        )
        company = (
            item.get("company")
            or (item.get("companyInfo") or {}).get("name")
            or item.get("companyName")
        )
        url = item.get("url") or item.get("externalApplyLink")
        job_key = (
            item.get("jobKey")
            or item.get("id")
            or (url.split("jk=")[-1].split("&")[0] if url and "jk=" in url else url)
        )

        if not (title and company and url):
            return None

        # location 可能是 string 或 dict
        loc_raw = item.get("location") or item.get("formattedLocation")
        if isinstance(loc_raw, dict):
            location = loc_raw.get("formattedLocation") or loc_raw.get("city") or ""
        else:
            location = str(loc_raw or "")

        # salary 同上
        salary_raw = item.get("salary") or item.get("salaryInfo")
        if isinstance(salary_raw, dict):
            salary = salary_raw.get("text") or salary_raw.get("formattedSalary")
        else:
            salary = str(salary_raw) if salary_raw else None

        return CollectedJob(
            source="indeed",
            external_id=str(job_key) if job_key else None,
            url=url,
            title=str(title).strip(),
            company=str(company).strip(),
            location=location or None,
            salary=salary or None,
            description=item.get("description") or item.get("descriptionText"),
            extras={
                "posted_at": item.get("postedAt") or item.get("postingDateParsed"),
                "rating": item.get("rating"),
                "reviews_count": item.get("reviewsCount"),
                "external_apply_link": item.get("externalApplyLink"),
            },
        )
