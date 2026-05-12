"""Y Combinator Work at a Startup via Apify (artemlazarevm/yc-jobs-scraper).

⚠️ This actor differs from typical scrapers - it returns data organized by "company", with each
item containing all jobs[] for that company. So we override search() to flatten the jobs.

Input schema (from official documentation):
    maxCompanies                Integer  max companies to scrape
    filterByBatch               List     ["W24","S24"] etc.
    filterByIndustry            List     ["B2B","AI","Fintech"]
    filterByStage               List     ["Seed","Series A"]
    filterByLocation            List     ["Remote","San Francisco"]
    topCompaniesOnly            Boolean
    includeFounderDescriptions  Boolean  (default true)
    rateLimitDelay              Number   (seconds, default 1.5)

Output each item (organized by company):
    {
      "company": {name, ycBatch, industry, teamSize, ...},
      "founders": [...],
      "jobs": [
        {
          "jobId", "title", "jobUrl", "location",
          "salary": {"min","max","currency"},
          "equity": {"min","max"},
          "jobType", "roleCategory", "experience", "visa",
          "skills": [...], "description", "interviewProcess",
          "applyUrl"
        },
        ...
      ]
    }

Note: keyword filtering must be done client-side (actor doesn't support title filtering).
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

import httpx

from ..config import Config
from .apify_base import ApifyCollector, ApifyError
from .base import BaseCollector, CollectedJob


# Translate candidate keywords to YC industry tags (YC's own classification has only ~50)
INDUSTRY_KEYWORD_MAP = {
    "ml": "AI",
    "machine learning": "AI",
    "ai": "AI",
    "llm": "AI",
    "infrastructure": "Developer Tools",
    "devops": "Developer Tools",
    "data": "B2B",
    "platform": "Developer Tools",
}


def _infer_industries(keywords: list[str]) -> list[str]:
    """Infer corresponding YC industry tags from user's job_titles."""
    industries: set[str] = set()
    for kw in keywords:
        kw_lower = kw.lower()
        for keyword_part, industry in INDUSTRY_KEYWORD_MAP.items():
            if keyword_part in kw_lower:
                industries.add(industry)
    return sorted(industries)


class YCApifyCollector(BaseCollector):
    """YC uses custom search flow: one API call gets company list, flatten jobs[]."""

    name = "yc"

    def __init__(self, config: Config):
        super().__init__(config)
        apify_cfg = config.raw.get("apify", {}) or {}
        token_env = apify_cfg.get("api_token_env", "APIFY_API_TOKEN")
        self._token = os.getenv(token_env)
        self._timeout_sec = int(apify_cfg.get("default_timeout_sec", 600))
        self._apify_cfg = (self._settings.get("apify") or {})

    @property
    def actor_id(self) -> str:
        return self._apify_cfg.get("actor", "artemlazarevm/yc-jobs-scraper")

    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        industries = _infer_industries(keywords)
        return {
            "maxCompanies": 20,                  # 50 too slow (2-5 min), reduced to 20
            "filterByLocation": locations,
            "filterByIndustry": industries or ["AI"],
            "topCompaniesOnly": False,
            "includeFounderDescriptions": False,
            "rateLimitDelay": 0.8,
        }

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        if not self._token:
            raise ApifyError("APIFY_API_TOKEN not set")

        input_data = self._build_input(keywords, locations)
        input_data.update(self._apify_cfg.get("input_overrides") or {})
        actor_path = self.actor_id.replace("/", "~")
        url = f"https://api.apify.com/v2/acts/{actor_path}/run-sync-get-dataset-items"

        print(f"  [apify] POST {self.actor_id}  input={input_data!r}")
        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(url, params={"token": self._token}, json=input_data)
        except httpx.HTTPError as e:
            raise ApifyError(f"Apify request failed: {e}")

        if resp.status_code >= 400:
            try:
                msg = resp.json()
            except Exception:
                msg = resp.text[:500]
            raise ApifyError(f"Apify Actor {self.actor_id} returned {resp.status_code}: {msg}")

        companies = resp.json()
        if not isinstance(companies, list):
            raise ApifyError(f"Apify returned non-array: {type(companies).__name__}")

        print(f"  [apify] received {len(companies)} companies")
        kws_lower = {k.lower() for k in (keywords or [])}
        count = 0

        # Flatten: expand each company's jobs, filter by keyword client-side
        for company_record in companies:
            if count >= self.max_per_run:
                break
            company = company_record.get("company") or {}
            company_name = company.get("name")
            jobs = company_record.get("jobs") or []

            for job in jobs:
                if count >= self.max_per_run:
                    break
                title = job.get("title") or ""
                # Client-side keyword filter (keep if any keyword matches title)
                if kws_lower and not any(k in title.lower() for k in kws_lower):
                    continue

                url_str = job.get("jobUrl") or job.get("applyUrl")
                if not url_str or not company_name:
                    continue

                salary_obj = job.get("salary") or {}
                if isinstance(salary_obj, dict) and salary_obj.get("min"):
                    salary = f"${salary_obj['min']:,} - ${salary_obj.get('max', '?'):,} {salary_obj.get('currency', 'USD')}"
                else:
                    salary = None

                yield CollectedJob(
                    source="yc",
                    external_id=str(job.get("jobId") or url_str),
                    url=url_str,
                    title=title.strip(),
                    company=company_name,
                    location=job.get("location"),
                    salary=salary,
                    description=job.get("description"),
                    extras={
                        "yc_batch": company.get("ycBatch"),
                        "industry": company.get("industry"),
                        "team_size": company.get("teamSize"),
                        "equity": job.get("equity"),
                        "visa": job.get("visa"),
                        "skills": job.get("skills"),
                        "apply_url": job.get("applyUrl"),
                    },
                )
                count += 1
