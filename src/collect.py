"""采集编排器: 跑所有启用的 collectors,把结果落到 DB,自动去重 + 关键词过滤."""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from sqlalchemy import select

from .collectors import CollectedJob, get_collector
from .config import Config
from .db import Job, JobStatus, session_scope


PLATFORMS = [
    "linkedin", "indeed", "glassdoor", "ziprecruiter",
    "yc", "wellfound", "dice", "hackernews",
]


def matches_excluded(cj: CollectedJob, excluded: list[str]) -> str | None:
    """如果岗位包含被排除的关键词,返回命中的关键词;否则 None.

    检查范围: title + description (大小写不敏感).
    借鉴 DailyJobMatch 的"采集时过滤"思路,在评分前就过滤掉明显不符合的,省 LLM 调用费.
    """
    if not excluded:
        return None
    haystack = f"{cj.title or ''}\n{cj.description or ''}".lower()
    for kw in excluded:
        kw_l = (kw or "").strip().lower()
        if kw_l and kw_l in haystack:
            return kw
    return None


def collect_all(config: Config, platforms: Optional[list[str]] = None) -> dict:
    """跑指定平台 (默认所有 enabled) 的采集器,返回统计."""
    platforms = platforms or PLATFORMS
    keywords = config.preferences.get("job_titles") or []
    locations = config.preferences.get("locations") or []
    if not keywords or not locations:
        raise RuntimeError("config.preferences 里需要先填 job_titles 和 locations")

    excluded = config.preferences.get("exclude_keywords") or []
    db_path = config.path("db_path")
    stats = {
        "total_new": 0,
        "total_seen": 0,
        "total_excluded": 0,
        "by_platform": {},
    }

    for platform in platforms:
        c = get_collector(platform, config)
        if not c.enabled:
            continue
        print(f"\n→ 采集 {platform}...")
        new_count = 0
        seen_count = 0
        excluded_count = 0
        try:
            for cj in c.search(keywords, locations):
                # 1) 排除关键词 (省 LLM 钱)
                hit = matches_excluded(cj, excluded)
                if hit:
                    excluded_count += 1
                    print(f"  [跳过] '{hit}' in {cj.title} @ {cj.company}")
                    continue

                # 2) 去重: 按 source + external_id 或 url
                with session_scope(db_path) as session:
                    stmt = select(Job)
                    if cj.external_id:
                        stmt = stmt.where(
                            Job.source == cj.source,
                            Job.external_id == cj.external_id,
                        )
                    else:
                        stmt = stmt.where(Job.url == cj.url)
                    existing = session.scalars(stmt).first()
                    if existing:
                        seen_count += 1
                        continue

                    # 3) 入库
                    job = Job(
                        source=cj.source,
                        external_id=cj.external_id,
                        url=cj.url,
                        title=cj.title,
                        company=cj.company,
                        location=cj.location,
                        salary=cj.salary,
                        description=cj.description,
                        status=JobStatus.NEW.value,
                    )
                    session.add(job)
                    session.commit()
                    new_count += 1
                    print(f"  + #{job.id} {cj.title} @ {cj.company}")
        except NotImplementedError as e:
            print(f"  [跳过] {e}")
        except Exception as e:
            print(f"  [错误] {platform}: {e}")

        stats["by_platform"][platform] = {
            "new": new_count,
            "duplicate": seen_count,
            "excluded": excluded_count,
        }
        stats["total_new"] += new_count
        stats["total_seen"] += seen_count
        stats["total_excluded"] += excluded_count

    return stats
