"""Collection orchestrator: runs all enabled collectors, stores results in DB, auto-deduplicates and filters by keywords."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from .collectors import CollectedJob, get_collector
from .config import Config
from .db import Job, JobStatus, session_scope
from .dedup import content_hash, dedup_key
from .profile_analyzer import load_profile


_EMPLOYMENT_TYPE_MAP = {
    # LinkedIn / HarvestAPI
    "full_time":   "full-time",
    "fulltime":    "full-time",
    "full-time":   "full-time",
    "part_time":   "part-time",
    "parttime":    "part-time",
    "part-time":   "part-time",
    "internship":  "internship",
    "intern":      "internship",
    "contract":    "contract",
    "contracts":   "contract",
    "temporary":   "contract",
    # Dice
    "fulltime":    "full-time",
    "parttime":    "part-time",
}


def _infer_job_type(cj: CollectedJob, job_types_override: list[str] | None) -> str:
    """Determine job_type from platform API data, title heuristics, or the active search filter."""
    # 1) Platform API field (most reliable)
    raw = (cj.extras or {}).get("employment_type") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    normalized = _EMPLOYMENT_TYPE_MAP.get(str(raw).lower().replace("-", "").replace("_", "").strip(), "")
    if normalized:
        return normalized

    # 2) Title heuristic: "intern" anywhere in title → internship
    if "intern" in cj.title.lower():
        return "internship"

    # 3) Fall back to the job_type the collector was told to search for
    if job_types_override:
        jt = job_types_override[0].lower()
        if "intern" in jt:
            return "internship"
        if "contract" in jt:
            return "contract"
        if "part" in jt:
            return "part-time"

    return "full-time"


def _parse_posted_at(cj: CollectedJob) -> Optional[datetime]:
    """Find published_at field in cj.extras, parse ISO format. Return None if not found."""
    if not cj.extras:
        return None
    # Different collectors use different keys, try all
    for key in ("posted_date", "postedDate", "posted_at", "postedAt", "posted", "published_at", "publishedAt"):
        val = cj.extras.get(key)
        if not val or not isinstance(val, str):
            continue
        try:
            s = val.replace("Z", "+00:00")
            # Strip milliseconds like ".000"
            return datetime.fromisoformat(s)
        except ValueError:
            continue
    return None


PLATFORMS = [
    "linkedin", "indeed", "glassdoor", "ziprecruiter",
    "yc", "wellfound", "dice", "hackernews",
]


def matches_excluded(cj: CollectedJob, excluded: list[str]) -> str | None:
    """If job contains excluded keywords, return the matching keyword; otherwise None.

    Check scope: title + description (case-insensitive).
    Following DailyJobMatch's "filter at collection" approach, filter obviously unsuitable jobs before scoring to save LLM calls.
    """
    if not excluded:
        return None
    haystack = f"{cj.title or ''}\n{cj.description or ''}".lower()
    for kw in excluded:
        kw_l = (kw or "").strip().lower()
        if kw_l and kw_l in haystack:
            return kw
    return None


def collect_all(
    config: Config,
    platforms: Optional[list[str]] = None,
    *,
    should_continue=None,
    profile_id: Optional[int] = None,
    on_platform_start=None,
    on_platform_done=None,   # callback(platform_name: str, new_count: int)
    job_types: Optional[list[str]] = None,  # Override config.yaml preferences.job_types
) -> dict:
    """Run collectors for specified platforms (default all enabled), return statistics.

    Search keywords priority:
    1. data/resume/_profile.json (Top-10 from analyze-profile + fuzzy expansion)
    2. config.yaml preferences.job_titles (fallback)

    should_continue: callable; called once before each platform, exit early if returns False
    profile_id: current active profile ID, tag new Jobs
    """
    if should_continue is None:
        should_continue = lambda: True

    platforms = platforms or PLATFORMS

    profile = load_profile(config)
    if profile and profile.top_10_positions:
        # First segment: 10 primary; second segment: aliases + broader_terms fuzzy expansion. Max 40.
        keywords = profile.search_titles(
            include_aliases=True, include_broader=True, limit=40
        )
        locations = (
            profile.target_locations
            or config.preferences.get("locations")
            or []
        )
        print(
            f"[collect] Using {len(keywords)} search keywords inferred from profile "
            f"(Top-10 primary + aliases + broader_terms): {keywords}"
        )
    else:
        keywords = config.preferences.get("job_titles") or []
        locations = config.preferences.get("locations") or []
        print(f"[collect] Using job_titles from config.yaml: {keywords}")

    if not keywords or not locations:
        raise RuntimeError(
            "No search keywords found — run `analyze-profile` first or fill job_titles + locations in config.yaml"
        )

    excluded = config.preferences.get("exclude_keywords") or []
    db_path = config.path("db_path")

    # job_types override: only set when explicitly passed, so collector can distinguish "user-specified" vs "config default"
    if job_types:
        print(f"[collect] job_types override: {job_types}")
    stats = {
        "total_new": 0,
        "total_seen": 0,
        "total_excluded": 0,
        "by_platform": {},
    }

    for platform in platforms:
        if not should_continue():
            print("[collect] Cancel signal detected, exiting early")
            break
        c = get_collector(platform, config)
        if not c.enabled:
            continue
        # Only inject override when explicitly passed, otherwise collector reads from config.preferences
        if job_types:
            c._job_types_override = job_types
        if on_platform_start:
            try:
                on_platform_start(platform)
            except Exception:
                pass
        print(f"\n→ Collecting from {platform}...")
        new_count = 0
        seen_count = 0
        excluded_count = 0
        try:
            for cj in c.search(keywords, locations):
                # 1) Exclude keywords (save LLM costs)
                hit = matches_excluded(cj, excluded)
                if hit:
                    excluded_count += 1
                    print(f"  [skip] '{hit}' in {cj.title} @ {cj.company}")
                    continue

                # 2) Deduplication: three-layer check
                chash = content_hash(cj.title, cj.company, cj.location or "")
                with session_scope(db_path) as session:
                    existing = None

                    # Layer 1: source + external_id exact match
                    if cj.external_id:
                        existing = session.scalars(
                            select(Job).where(
                                Job.source == cj.source,
                                Job.external_id == cj.external_id,
                            )
                        ).first()

                    # Layer 2: URL exact match (same URL across platforms)
                    if not existing and cj.url:
                        existing = session.scalars(
                            select(Job).where(Job.url == cj.url)
                        ).first()

                    # Layer 3: content_hash semantic dedup (same job across platforms, different URLs)
                    if not existing:
                        existing = session.scalars(
                            select(Job).where(Job.content_hash == chash)
                        ).first()
                        if existing:
                            print(f"  [semantic duplicate] {cj.title} @ {cj.company} "
                                  f"≈ #{existing.id} ({existing.source}) "
                                  f"key={dedup_key(cj.title, cj.company, cj.location or '')}")

                    if existing:
                        seen_count += 1
                        continue

                    # 3) Store in database
                    _jt_override = getattr(c, "_job_types_override", None)
                    job = Job(
                        source=cj.source,
                        external_id=cj.external_id,
                        url=cj.url,
                        title=cj.title,
                        company=cj.company,
                        location=cj.location,
                        salary=cj.salary,
                        description=cj.description,
                        posted_at=_parse_posted_at(cj),
                        status=JobStatus.NEW.value,
                        profile_id=profile_id,
                        content_hash=chash,
                        job_type=_infer_job_type(cj, _jt_override),
                    )
                    session.add(job)
                    session.commit()
                    new_count += 1
                    print(f"  + #{job.id} {cj.title} @ {cj.company}")
        except NotImplementedError as e:
            print(f"  [skip] {e}")
        except Exception as e:
            print(f"  [error] {platform}: {e}")

        stats["by_platform"][platform] = {
            "new": new_count,
            "duplicate": seen_count,
            "excluded": excluded_count,
        }
        stats["total_new"] += new_count
        stats["total_seen"] += seen_count
        stats["total_excluded"] += excluded_count
        if on_platform_done:
            try:
                on_platform_done(platform, new_count)
            except Exception:
                pass

    return stats
