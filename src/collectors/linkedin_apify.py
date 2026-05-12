"""LinkedIn 通过 Apify (默认用 harvestapi/linkedin-job-search).

为什么选 harvestapi:
- $1 / 1000 jobs (Apify 上最便宜)
- 无月费 / 无需登录 cookies
- API 稳定,无 Cloudflare 问题

搜索策略 (per-title 模式):
  profile_analyzer 生成 Top-10 positions, 每个 position 带 aliases + broader_terms,
  共约 40 个搜索词. 我们对**每个 title 单独发一次 Apify 请求**, 每次返回
  results_per_title 条 (默认 15). 跨 title 按 URL 去重, 最终最多入库
  max_per_run 条.

  好处:
  - 每个 title 都有独立的搜索配额, 不会 40 个 title 抢 24 条结果
  - "ML Engineer" / "Machine Learning Engineer" / "AI/ML Engineer" 各自返回
    最新的 15 条, 覆盖面大大提升
  - 跨 title 去重后, 相同岗位只算一次

费用估算 (harvestapi $1/1000 jobs):
  40 titles × 15 条 = 600 条原始结果 ≈ $0.60/次
  config 里可以调 results_per_title 和 max_titles 来控制成本

Actor input schema (https://apify.com/harvestapi/linkedin-job-search):
    jobTitles:       List[str]    岗位关键词 (必填)
    locations:       List[str]    地点列表
    sortBy:          "relevance" | "date"
    workplaceType:   List["Remote"|"Hybrid"|"On-site"]
    employmentType:  List["full-time"|"part-time"|"contract"|"internship"|"temporary"]
    experienceLevel: List["Entry Level"|"Mid Level"|"Senior Level"]
    postedLimit:     "1h"|"24h"|"week"|"month"
    maxItems:        int     这次搜索最多返回多少条
"""
from __future__ import annotations

from typing import Iterable, Optional

from .apify_base import ApifyCollector, ApifyError
from .base import CollectedJob


def _hours_to_posted_limit(hours: int) -> str:
    if hours <= 1:   return "1h"
    if hours <= 24:  return "24h"
    if hours <= 168: return "week"
    return "month"


def _normalize_location(locations: list[str]) -> list[str]:
    """如果包含 'United States', 只用它即可覆盖全美, 不需要额外加城市.
    这样可以避免同一岗位因城市不同被重复搜到.
    """
    for loc in locations:
        if "united states" in loc.lower() or loc.strip().upper() == "US":
            return ["United States"]
    return locations or ["United States"]


class LinkedInApifyCollector(ApifyCollector):
    name = "linkedin"

    @property
    def results_per_title(self) -> int:
        """每个 title 单独搜索时返回的条数. 可在 config.yaml 里覆盖."""
        return int(self._settings.get("results_per_title", 15))

    @property
    def max_titles(self) -> int:
        """最多搜索多少个 title (防止 40 个 title 花太多时间/费用).
        默认 40, 可在 config.yaml 里限制."""
        return int(self._settings.get("max_titles", 40))

    @property
    def employment_types(self) -> list[str]:
        """job_types → harvestapi employmentType 格式.
        优先读 collect_all 注入的实例覆盖值，否则读 config.yaml（不修改共享对象）。
        """
        raw = getattr(self, "_job_types_override", None) \
              or self.config.preferences.get("job_types") \
              or ["Full-time"]
        # 标准化映射 — harvestapi 要求全小写
        mapping = {
            "full-time": "full-time",
            "full_time": "full-time",
            "fulltime":  "full-time",
            "part-time": "part-time",
            "part_time": "part-time",
            "parttime":  "part-time",
            "contract":  "contract",
            "internship":"internship",
            "intern":    "internship",
            "temporary": "temporary",
            "temp":      "temporary",
            "volunteer": "volunteer",
            "other":     "other",
        }
        result = []
        for t in raw:
            normalized = mapping.get(t.lower().strip(), t)
            if normalized not in result:
                result.append(normalized)
        return result

    def _build_single_input(self, title: str, locations: list[str]) -> dict:
        """为单个 title 构建 Apify 请求 input."""
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 24)
        inp = {
            "jobTitles": [title],
            "locations": locations,
            "sortBy": "date",
            "postedLimit": _hours_to_posted_limit(max_age_hours),
            "maxItems": self.results_per_title,
        }
        emp_types = self.employment_types
        if emp_types:
            inp["employmentType"] = emp_types
        return inp


    # _build_input 保留兼容性 (基类 search() 不再用, 但其他代码可能调用)
    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        max_age_hours = int(self.config.freshness.get("max_age_hours", 0) or 24)
        return {
            "jobTitles": keywords,
            "locations": locations,
            "sortBy": "date",
            "postedLimit": _hours_to_posted_limit(max_age_hours),
            "maxItems": self.results_per_title,
        }

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        """Per-title 搜索: 每个 title 单独一次 Apify 调用, 跨 title 按 URL 去重."""
        if not self._token:
            raise ApifyError(
                "APIFY_API_TOKEN 未设置. 去 https://console.apify.com/account/integrations 拿一个,填到 .env"
            )

        norm_locations = _normalize_location(locations)
        titles = keywords[: self.max_titles]
        total_cap = self.max_per_run          # 全局上限, 跨所有 title
        seen_urls: set[str] = set()           # 跨 title 去重
        yielded = 0

        print(f"  [linkedin] per-title 模式: {len(titles)} 个 title × "
              f"{self.results_per_title} 条/title, 地点={norm_locations}, "
              f"总上限={total_cap}")

        for i, title in enumerate(titles, 1):
            if yielded >= total_cap:
                print(f"  [linkedin] 已达总上限 {total_cap}, 停止")
                break

            input_data = self._build_single_input(title, norm_locations)
            input_data.update(self.input_overrides)

            print(f"  [linkedin] ({i}/{len(titles)}) 搜索: {title!r} ...")
            try:
                items = self._run_actor(input_data)
            except ApifyError as e:
                print(f"  [linkedin] [{title}] Apify 错误, 跳过: {e}")
                continue

            new_this_title = 0
            for item in items:
                if yielded >= total_cap:
                    break
                try:
                    cj = self._parse_item(item)
                except Exception as e:
                    print(f"  [linkedin] 解析失败,跳过: {e}")
                    continue
                if cj is None:
                    continue
                # 跨 title 去重
                key = cj.url or cj.external_id or ""
                if key and key in seen_urls:
                    continue
                if key:
                    seen_urls.add(key)
                yield cj
                yielded += 1
                new_this_title += 1

            print(f"  [linkedin] [{title}] +{new_this_title} 条 (累计 {yielded}/{total_cap})")

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        title = item.get("title")
        url = item.get("linkedinUrl") or item.get("url")

        company_obj = item.get("company") or {}
        company = (
            company_obj.get("name")
            if isinstance(company_obj, dict)
            else str(company_obj)
        )

        if not (title and company and url):
            return None

        loc_obj = item.get("location") or {}
        if isinstance(loc_obj, dict):
            location = (
                loc_obj.get("linkedinText")
                or (loc_obj.get("parsed") or {}).get("text")
                or ""
            )
        else:
            location = str(loc_obj or "")

        salary_obj = item.get("salary") or {}
        salary = salary_obj.get("text") if isinstance(salary_obj, dict) else (
            str(salary_obj) if salary_obj else None
        )

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
                "posted_date":     item.get("postedDate"),
                "employment_type": item.get("employmentType"),
                "workplace_type":  item.get("workplaceType"),
                "applicants":      item.get("applicants"),
                "apply_url":       (item.get("applyMethod") or {}).get("companyApplyUrl"),
            },
        )
