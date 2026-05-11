"""LinkedIn 通过 Apify (默认用 harvestapi/linkedin-job-search).

为什么选 harvestapi:
- $1 / 1000 jobs (Apify 上最便宜)
- 无月费 / 无需登录 cookies
- API 稳定,无 Cloudflare 问题

Actor input schema (https://apify.com/harvestapi/linkedin-job-search):
    search:          List[str]    岗位关键词 (必填)
    locations:       List[str]    地点列表
    sortBy:          "relevance" | "date"
    workplaceType:   List["Remote"|"Hybrid"|"On-site"]
    employmentType:  List["Full-time"|"Part-time"|"Contract"|...]
    experienceLevel: List["Entry Level"|"Mid Level"|"Senior Level"]
    postedLimit:     "Past hour"|"Past 24 hours"|"Past Week"|"Past Month"
    maxItems:        int     每个搜索 query 最多返回多少

Output 每条:
    {
        "id": "4227647589",
        "title": "...",
        "linkedinUrl": "https://www.linkedin.com/jobs/view/...",
        "descriptionText": "...",
        "location": {"linkedinText": "Greenwood Village, CO", ...},
        "salary": {"text": "80,000 - 85,000 USD", "min": 80000, ...},
        "company": {"name": "East Daley Analytics", ...},
        "postedDate": "2025-05-14T...",
        "employmentType": "full_time",
        "workplaceType": "on_site",
        ...
    }

要换别家 Actor (如 bebity, curious_coder),只要改 _build_input 和 _parse_item 即可.
"""
from __future__ import annotations

from typing import Optional

from .apify_base import ApifyCollector
from .base import CollectedJob


def _hours_to_posted_limit(hours: int) -> str:
    """harvestapi 接受 1h / 24h / week / month"""
    if hours <= 1:
        return "1h"
    if hours <= 24:
        return "24h"
    if hours <= 24 * 7:
        return "week"
    return "month"


class LinkedInApifyCollector(ApifyCollector):
    name = "linkedin"

    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        """harvestapi 实际字段名 (从 console.apify.com 的 input 页面确认):
        jobTitles, locations, maxItems, sortBy, postedLimit,
        company, industryIds, easyApply, under10Applicants
        """
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 24)
        per_query = max(1, self.max_per_run // max(1, len(keywords) * len(locations)))

        return {
            "jobTitles": keywords,            # ← 关键字段名
            "locations": locations,
            "sortBy": "date",                 # date | relevance
            "postedLimit": _hours_to_posted_limit(max_age_hours),
            "maxItems": per_query,
        }

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        title = item.get("title")
        url = item.get("linkedinUrl") or item.get("url")

        # company 是嵌套对象
        company_obj = item.get("company") or {}
        company = (
            company_obj.get("name")
            if isinstance(company_obj, dict)
            else str(company_obj)
        )

        if not (title and company and url):
            return None

        # location 是嵌套对象
        loc_obj = item.get("location") or {}
        if isinstance(loc_obj, dict):
            location = (
                loc_obj.get("linkedinText")
                or (loc_obj.get("parsed") or {}).get("text")
                or ""
            )
        else:
            location = str(loc_obj or "")

        # salary 是嵌套对象
        salary_obj = item.get("salary") or {}
        if isinstance(salary_obj, dict):
            salary = salary_obj.get("text")
        else:
            salary = str(salary_obj) if salary_obj else None

        return CollectedJob(
            source="linkedin",
            external_id=str(item.get("id") or url),
            url=url,
            title=str(title).strip(),
            company=str(company).strip(),
            location=location or None,
            salary=salary or None,
            description=item.get("descriptionText") or item.get("description"),
            extras={
                "posted_date": item.get("postedDate"),
                "employment_type": item.get("employmentType"),
                "workplace_type": item.get("workplaceType"),
                "applicants": item.get("applicants"),
                "apply_url": (item.get("applyMethod") or {}).get("companyApplyUrl"),
            },
        )
