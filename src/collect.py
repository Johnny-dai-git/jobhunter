"""采集编排器: 跑所有启用的 collectors,把结果落到 DB,自动去重 + 关键词过滤."""
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


def _parse_posted_at(cj: CollectedJob) -> Optional[datetime]:
    """从 cj.extras 找发布时间字段, 解析 ISO. 找不到返回 None."""
    if not cj.extras:
        return None
    # 各 collector 用不同 key, 都试一遍
    for key in ("posted_date", "postedDate", "posted_at", "postedAt", "posted", "published_at", "publishedAt"):
        val = cj.extras.get(key)
        if not val or not isinstance(val, str):
            continue
        try:
            s = val.replace("Z", "+00:00")
            # 砍掉 ".000" 这种毫秒
            return datetime.fromisoformat(s)
        except ValueError:
            continue
    return None


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


def collect_all(
    config: Config,
    platforms: Optional[list[str]] = None,
    *,
    should_continue=None,
    profile_id: Optional[int] = None,
    on_platform_start=None,
    on_platform_done=None,   # callback(platform_name: str, new_count: int)
) -> dict:
    """跑指定平台 (默认所有 enabled) 的采集器,返回统计.

    搜索 keywords 来源优先级:
    1. data/resume/_profile.json (analyze-profile 生成的 Top-10 + 模糊扩展)
    2. config.yaml preferences.job_titles (fallback)

    should_continue: callable; 每个平台开始前调用一次, 返回 False 就提前退出
    profile_id: 当前活跃画像的 ID, 给新入库的 Job 打标
    """
    if should_continue is None:
        should_continue = lambda: True

    platforms = platforms or PLATFORMS

    profile = load_profile(config)
    if profile and profile.top_10_positions:
        # 第一段: 10 个 primary; 第二段: aliases + broader_terms 模糊扩展. 上限 40.
        keywords = profile.search_titles(
            include_aliases=True, include_broader=True, limit=40
        )
        locations = (
            profile.target_locations
            or config.preferences.get("locations")
            or []
        )
        print(
            f"[collect] 用 profile 推断的 {len(keywords)} 个搜索词 "
            f"(Top-10 primary + aliases + broader_terms): {keywords}"
        )
    else:
        keywords = config.preferences.get("job_titles") or []
        locations = config.preferences.get("locations") or []
        print(f"[collect] 用 config.yaml 里的 job_titles: {keywords}")

    if not keywords or not locations:
        raise RuntimeError(
            "找不到搜索关键词 — 先跑 `analyze-profile` 或在 config.yaml 里填 job_titles + locations"
        )

    excluded = config.preferences.get("exclude_keywords") or []
    db_path = config.path("db_path")
    stats = {
        "total_new": 0,
        "total_seen": 0,
        "total_excluded": 0,
        "by_platform": {},
    }

    for platform in platforms:
        if not should_continue():
            print("[collect] 检测到取消信号, 提前退出")
            break
        c = get_collector(platform, config)
        if not c.enabled:
            continue
        if on_platform_start:
            try:
                on_platform_start(platform)
            except Exception:
                pass
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

                # 2) 去重: 三层检查
                chash = content_hash(cj.title, cj.company, cj.location or "")
                with session_scope(db_path) as session:
                    existing = None

                    # 层1: source + external_id 精确匹配
                    if cj.external_id:
                        existing = session.scalars(
                            select(Job).where(
                                Job.source == cj.source,
                                Job.external_id == cj.external_id,
                            )
                        ).first()

                    # 层2: URL 精确匹配 (跨平台同 URL)
                    if not existing and cj.url:
                        existing = session.scalars(
                            select(Job).where(Job.url == cj.url)
                        ).first()

                    # 层3: content_hash 语义去重 (跨平台同岗位, URL 不同)
                    if not existing:
                        existing = session.scalars(
                            select(Job).where(Job.content_hash == chash)
                        ).first()
                        if existing:
                            print(f"  [语义重复] {cj.title} @ {cj.company} "
                                  f"≈ #{existing.id} ({existing.source}) "
                                  f"key={dedup_key(cj.title, cj.company, cj.location or '')}")

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
                        posted_at=_parse_posted_at(cj),
                        status=JobStatus.NEW.value,
                        profile_id=profile_id,
                        content_hash=chash,
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
        if on_platform_done:
            try:
                on_platform_done(platform, new_count)
            except Exception:
                pass

    return stats
