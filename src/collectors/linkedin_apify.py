"""LinkedIn via Apify (default: harvestapi/linkedin-job-search).

Why harvestapi:
- $1 / 1000 jobs (cheapest on Apify)
- No monthly fee / no need for login cookies
- Stable API, no Cloudflare issues

Search strategy (per-title mode):
  profile_analyzer generates Top-10 positions, each with aliases + broader_terms,
  totaling ~40 search terms. We make **one Apify request per title**, each returning
  results_per_title items (default 15). Cross-title deduplication by URL, final max
  max_per_run items.

  Benefits:
  - Each title has independent search quota, no 40 titles competing for 24 results
  - "ML Engineer" / "Machine Learning Engineer" / "AI/ML Engineer" each return
    latest 15 items, greatly expanding coverage
  - After cross-title dedup, each job counted once

Cost estimate (harvestapi $1/1000 jobs):
  40 titles × 15 items = 600 raw results ≈ $0.60/run
  Can adjust results_per_title and max_titles in config to control cost

Actor input schema (https://apify.com/harvestapi/linkedin-job-search):
    jobTitles:       List[str]    job keywords (required)
    locations:       List[str]    location list
    sortBy:          "relevance" | "date"
    workplaceType:   List["Remote"|"Hybrid"|"On-site"]
    employmentType:  List["full-time"|"part-time"|"contract"|"internship"|"temporary"]
    experienceLevel: List["Entry Level"|"Mid Level"|"Senior Level"]
    postedLimit:     "1h"|"24h"|"week"|"month"
    maxItems:        int     max items to return from this search
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
    """If 'United States' is included, only use it to cover whole US, no need for additional cities.
    This avoids the same job being found multiple times due to different cities.
    """
    for loc in locations:
        if "united states" in loc.lower() or loc.strip().upper() == "US":
            return ["United States"]
    return locations or ["United States"]


class LinkedInApifyCollector(ApifyCollector):
    name = "linkedin"

    @property
    def results_per_title(self) -> int:
        """Items returned per title when searching separately. Can be overridden in config.yaml."""
        return int(self._settings.get("results_per_title", 15))

    @property
    def max_titles(self) -> int:
        """Max titles to search (prevent 40 titles from taking too much time/cost).
        Default 40, can be limited in config.yaml."""
        return int(self._settings.get("max_titles", 40))

    @property
    def employment_types(self) -> list[str]:
        """job_types → harvestapi employmentType format.
        Priority: instance override from collect_all, then config.yaml (don't modify shared object).
        """
        raw = getattr(self, "_job_types_override", None) \
              or self.config.preferences.get("job_types") \
              or ["Full-time"]
        # Normalization mapping — harvestapi requires lowercase
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
        """Build Apify request input for a single title."""
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


    # _build_input kept for compatibility (base class search() no longer uses it, but other code may)
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
        """Per-title search: one Apify call per title, cross-title dedup by URL."""
        if not self._token:
            raise ApifyError(
                "APIFY_API_TOKEN not set. Get one at https://console.apify.com/account/integrations and add to .env"
            )

        norm_locations = _normalize_location(locations)
        titles = keywords[: self.max_titles]
        total_cap = self.max_per_run          # Global cap across all titles
        seen_urls: set[str] = set()           # Cross-title dedup
        yielded = 0

        print(f"  [linkedin] per-title mode: {len(titles)} titles × "
              f"{self.results_per_title} items/title, locations={norm_locations}, "
              f"total_cap={total_cap}")

        for i, title in enumerate(titles, 1):
            if yielded >= total_cap:
                print(f"  [linkedin] reached total cap {total_cap}, stopping")
                break

            input_data = self._build_single_input(title, norm_locations)
            input_data.update(self.input_overrides)

            print(f"  [linkedin] ({i}/{len(titles)}) search: {title!r} ...")
            try:
                items = self._run_actor(input_data)
            except ApifyError as e:
                print(f"  [linkedin] [{title}] Apify error, skipping: {e}")
                continue

            new_this_title = 0
            for item in items:
                if yielded >= total_cap:
                    break
                try:
                    cj = self._parse_item(item)
                except Exception as e:
                    print(f"  [linkedin] parse failed, skipping: {e}")
                    continue
                if cj is None:
                    continue
                # Cross-title dedup
                key = cj.url or cj.external_id or ""
                if key and key in seen_urls:
                    continue
                if key:
                    seen_urls.add(key)
                yield cj
                yielded += 1
                new_this_title += 1

            print(f"  [linkedin] [{title}] +{new_this_title} items (total {yielded}/{total_cap})")

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
