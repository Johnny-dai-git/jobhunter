"""Y Combinator Work at a Startup via Apify (artemlazarevm/yc-jobs-scraper).

⚠️ 这个 actor 跟一般 scraper 不同 - 它按"公司"返回数据,每条 item 里嵌套着该公司的
所有 jobs[]. 所以我们要重写 search() 把 jobs 平铺出来.

Input schema (从官方文档):
    maxCompanies                Integer  最大抓取公司数
    filterByBatch               List     ["W24","S24"] 等
    filterByIndustry            List     ["B2B","AI","Fintech"]
    filterByStage               List     ["Seed","Series A"]
    filterByLocation            List     ["Remote","San Francisco"]
    topCompaniesOnly            Boolean
    includeFounderDescriptions  Boolean  (默认 true)
    rateLimitDelay              Number   (秒,默认 1.5)

Output 每条 (按公司组织):
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

注意 keyword 过滤要在客户端做(actor 不支持按 title 过滤).
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

import httpx

from ..config import Config
from .apify_base import ApifyCollector, ApifyError
from .base import BaseCollector, CollectedJob


# 把候选人的关键词翻成 YC industry 标签 (YC 自己的分类只有几十个)
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
    """从用户的 job_titles 推断对应的 YC industry tag."""
    industries: set[str] = set()
    for kw in keywords:
        kw_lower = kw.lower()
        for keyword_part, industry in INDUSTRY_KEYWORD_MAP.items():
            if keyword_part in kw_lower:
                industries.add(industry)
    return sorted(industries)


class YCApifyCollector(BaseCollector):
    """YC 用自定义 search 流程: 一次 API call 拿公司列表,平铺 jobs[]."""

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
            "maxCompanies": 20,                  # 50 太慢 (2-5 分钟), 降到 20
            "filterByLocation": locations,
            "filterByIndustry": industries or ["AI"],
            "topCompaniesOnly": False,
            "includeFounderDescriptions": False,
            "rateLimitDelay": 0.8,
        }

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        if not self._token:
            raise ApifyError("APIFY_API_TOKEN 未设置")

        input_data = self._build_input(keywords, locations)
        input_data.update(self._apify_cfg.get("input_overrides") or {})
        actor_path = self.actor_id.replace("/", "~")
        url = f"https://api.apify.com/v2/acts/{actor_path}/run-sync-get-dataset-items"

        print(f"  [apify] POST {self.actor_id}  input={input_data!r}")
        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(url, params={"token": self._token}, json=input_data)
        except httpx.HTTPError as e:
            raise ApifyError(f"Apify 请求失败: {e}")

        if resp.status_code >= 400:
            try:
                msg = resp.json()
            except Exception:
                msg = resp.text[:500]
            raise ApifyError(f"Apify Actor {self.actor_id} 返回 {resp.status_code}: {msg}")

        companies = resp.json()
        if not isinstance(companies, list):
            raise ApifyError(f"Apify 返回非数组: {type(companies).__name__}")

        print(f"  [apify] 拿回 {len(companies)} 个公司")
        kws_lower = {k.lower() for k in (keywords or [])}
        count = 0

        # 平铺: 每个公司里的 jobs 都展开,按 keyword 客户端过滤
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
                # 客户端关键词过滤 (任一 keyword 命中 title 就保留)
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
