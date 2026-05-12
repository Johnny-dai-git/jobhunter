"""Indeed via Apify (misceres/indeed-scraper).

Actor schema (https://apify.com/misceres/indeed-scraper/input-schema):
    position           - single string (single keyword)
    location           - single string
    country            - enum, default 'US'
    maxItemsPerSearch  - max per search
    parseCompanyDetails - whether to scrape company details
    saveOnlyUniqueItems - auto-deduplicate
    startUrls          - array of URLs, supports multiple search URLs

We use startUrls approach: generate N search URLs (keyword × location combinations),
complete all searches in one actor call. Cost ~$0.005/job.

Output each item (common fields with fallback compatibility):
    {
        "positionName": "...",     or "position", "title"
        "company": "...",
        "location": "...",
        "description": "...",
        "salary": "...",
        "url": "...",              detail page URL
        "jobKey": "..."
    }
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from .apify_base import ApifyCollector
from .base import CollectedJob


def _hours_to_fromage(hours: int) -> str:
    """Indeed uses fromage=1/3/7/14 to represent past 1/3/7/14 days."""
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

        # Generate N search URLs, one per (keyword, location) pair
        start_urls = []
        for kw in keywords:
            for loc in locations:
                params = {"q": kw, "l": loc, "fromage": fromage}
                start_urls.append({
                    "url": f"https://www.indeed.com/jobs?{urlencode(params)}"
                })

        # Each URL returns results_per_url items (default 15, to match LinkedIn)
        # max_per_run is the local total cap (applied in apify_base.search()), independent of per-URL count
        results_per_url = int(self._settings.get("results_per_url", 15))

        return {
            "startUrls": start_urls,
            "maxItemsPerSearch": results_per_url,
            "country": "US",
            "parseCompanyDetails": False,
            "saveOnlyUniqueItems": True,
        }

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        # misceres output fields are not fixed, use fallback
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

        # location may be string or dict
        loc_raw = item.get("location") or item.get("formattedLocation")
        if isinstance(loc_raw, dict):
            location = loc_raw.get("formattedLocation") or loc_raw.get("city") or ""
        else:
            location = str(loc_raw or "")

        # salary same as above
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
