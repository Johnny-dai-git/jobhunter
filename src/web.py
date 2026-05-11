"""本地 web UI: 查看岗位 + 点击触发 Claude 简历定制 + PDF.

启动: `python -m src.main web` -> http://127.0.0.1:8765

为了避开 fastapi/starlette/jinja2 版本冲突坑, 这版**不用 Jinja2Templates**,
直接用 jinja2 自己渲染字符串后塞回 HTMLResponse.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

import os
from datetime import timedelta

from fastapi.responses import JSONResponse

from . import agent_state
from .collect import collect_all
from .config import Config
from .cover_letter import write_cover_letter
from .db import Job, JobStatus, init_db, session_scope
from .matcher import score_pending
from .profile_analyzer import (
    activate_profile_snapshot,
    analyze_profile,
    get_current_profile_id,
    list_profile_snapshots,
    load_profile,
    load_user_description,
    save_profile,
    save_profile_snapshot,
    save_user_description,
)
from .cron_manager import (
    HOURS_TO_CRON,
    crontab_available,
    get_status as cron_status,
    install as cron_install,
    uninstall as cron_uninstall,
)
from .resume_reader import (
    SUPPORTED_EXTS,
    delete_paused_resume,
    list_paused_resumes,
    list_resumes,
    load_cached,
    parse_and_cache,
    pause_resume_file,
    unpause_resume_file,
)
from .scheduler import AgentScheduler
from .tailor import tailor_for_job
from .tracker import mark_applied


TEMPLATES_DIR = Path(__file__).resolve().parent / "web_templates"


# ========= 排序辅助 =========
_EDU_RANK = {
    "phd": 5,
    "master": 4,
    "bachelor": 3,
    "high_school": 2,
    "any": 1,
    "unspecified": 0,
    "": 0,
    None: 0,
}


def _edu_rank(val: str | None) -> int:
    return _EDU_RANK.get((val or "").lower(), 0)


def _parse_salary_min(s: str | None) -> int:
    """从'$120,000 - $150,000' / '80K - 110K USD' 等字符串里抽 minimum 数字 (USD).

    用于排序; 抓不到就返回 0.
    """
    import re
    if not s:
        return 0
    nums: list[int] = []
    for m in re.finditer(r"\$?\s*(\d{1,3}(?:[,\.]\d{3})+|\d+)\s*([Kk])?", s):
        raw = m.group(1).replace(",", "").replace(".", "")
        try:
            n = int(raw)
        except ValueError:
            continue
        if m.group(2):  # K/k 后缀
            n *= 1000
        elif n < 1000:  # 纯数字 < 1000 (例如 "120") 视作 K
            n *= 1000
        if 10_000 <= n <= 1_500_000:
            nums.append(n)
    return min(nums) if nums else 0


_MODE_RANK = {"remote": 3, "hybrid": 2, "onsite": 1, "unspecified": 0, "": 0, None: 0}


def _mode_rank(val: str | None) -> int:
    return _MODE_RANK.get((val or "").lower(), 0)


SORTERS = {
    "score": lambda j: -(j.match_score or -1),
    "salary": lambda j: -_parse_salary_min(j.salary),
    "education": lambda j: -_edu_rank(j.min_education),
    "mode": lambda j: -_mode_rank(j.work_mode),
    "posted": lambda j: -(j.posted_at.timestamp() if j.posted_at else 0),
    "company": lambda j: (j.company or "").lower(),
}


def _make_env() -> Environment:
    """直接构造 Jinja2 Environment, 不走 starlette wrapper, 避开 cache_key bug."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        cache_size=0,  # 禁用缓存彻底避坑
    )


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="JobHunter")
    env = _make_env()
    db_path = config.path("db_path")

    # ===== 后台流水线状态 (单实例够用,多用户场景需换 DB) =====
    pipeline_state: dict[str, Any] = {
        "running": False,
        "phase": "idle",            # idle | analyzing | collecting | matching | done | cancelled | error
        "started_at": None,
        "ended_at": None,
        "error": None,
        "stats": {},
        "profile_id": None,         # 当前流水线绑定的画像 id
        "cancel_requested": False,
        "current_platform": None,   # 采集阶段正在跑哪个平台
        "platform_started_at": None,
    }
    pipeline_lock = threading.Lock()

    def _should_continue():
        return not pipeline_state["cancel_requested"]

    def _run_pipeline_bg(
        do_analyze: bool,
        user_description: str | None = None,
        resume_filename: str | None = None,
    ):
        """后台跑 [analyze_profile] -> collect -> match. 失败也能 graceful exit."""
        with pipeline_lock:
            if pipeline_state["running"]:
                return  # 防止重入
            pipeline_state.update(
                running=True,
                phase="analyzing" if do_analyze else "collecting",
                started_at=datetime.now(),
                ended_at=None,
                error=None,
                stats={},
                cancel_requested=False,
                profile_id=None,
            )

        # 同步写持久化状态 (供其他进程/web session 看)
        try:
            agent_state.start_run(config, trigger="web")
            agent_state.update_phase(config, pipeline_state["phase"])
        except Exception:
            pass

        try:
            if do_analyze:
                profile = analyze_profile(config, user_description=user_description)
                save_profile(config, profile)
                # 同时写历史 DB
                pid = save_profile_snapshot(
                    config, profile,
                    user_description=user_description or "",
                    resume_filename=resume_filename,
                )
                pipeline_state["profile_id"] = pid
                # 同步当前画像信息到持久化状态
                try:
                    agent_state.update_phase(
                        config, pipeline_state["phase"],
                        profile_id=pid,
                        profile_label=profile.summary[:80] if profile.summary else None,
                    )
                except Exception:
                    pass
            else:
                # 沿用现有当前画像 id
                pipeline_state["profile_id"] = get_current_profile_id(config)
                # 拿 label
                try:
                    if pipeline_state["profile_id"]:
                        from .db import Profile
                        with session_scope(db_path) as session:
                            row = session.get(Profile, pipeline_state["profile_id"])
                            label = row.label if row else None
                        agent_state.update_phase(
                            config, pipeline_state["phase"],
                            profile_id=pipeline_state["profile_id"],
                            profile_label=label,
                        )
                except Exception:
                    pass

            if not _should_continue():
                pipeline_state["phase"] = "cancelled"
                return

            pipeline_state["phase"] = "collecting"
            try: agent_state.update_phase(config, "collecting")
            except Exception: pass

            def _on_platform(name: str):
                pipeline_state["current_platform"] = name
                pipeline_state["platform_started_at"] = datetime.now()
                try: agent_state.set_platform(config, name)
                except Exception: pass

            stats = collect_all(
                config,
                should_continue=_should_continue,
                profile_id=pipeline_state["profile_id"],
                on_platform_start=_on_platform,
            )
            pipeline_state["stats"]["collect"] = stats
            pipeline_state["current_platform"] = None
            try:
                agent_state.set_platform(config, None)
                agent_state.update_phase(config, "collecting", stats={"collect": stats})
            except Exception: pass

            if not _should_continue():
                pipeline_state["phase"] = "cancelled"
                return

            pipeline_state["phase"] = "matching"
            try: agent_state.update_phase(config, "matching")
            except Exception: pass

            resume_text = load_cached(config.path("resume_dir"))
            results = score_pending(config, resume_text, should_continue=_should_continue)
            pipeline_state["stats"]["match"] = {"scored": len(results)}

            final_phase = "cancelled" if not _should_continue() else "done"
            pipeline_state["phase"] = final_phase
            try:
                agent_state.update_phase(config, final_phase, stats={"match": {"scored": len(results)}})
            except Exception: pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            pipeline_state["phase"] = "error"
            pipeline_state["error"] = str(e)
        finally:
            pipeline_state["running"] = False
            pipeline_state["ended_at"] = datetime.now()
            # 写最终持久化状态
            try:
                if pipeline_state.get("phase") == "error":
                    agent_state.end_run(config, success=False, phase="error", error=pipeline_state.get("error"))
                elif pipeline_state.get("phase") == "cancelled":
                    agent_state.end_run(config, success=False, phase="cancelled")
                else:
                    agent_state.end_run(config, success=True, phase="done")
            except Exception:
                pass


    def _wait_for_cancel(timeout: float = 30) -> bool:
        """请求取消,轮询等到流水线真停下来(或超时). 返回是否成功停下."""
        import time as _time
        if not pipeline_state["running"]:
            return True
        pipeline_state["cancel_requested"] = True
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if not pipeline_state["running"]:
                return True
            _time.sleep(0.5)
        return False

    # ===== 自动调度 =====
    def _scheduled_run():
        """scheduler 触发的回调: 用当前画像跑一次 collect+match (不阻塞调用方)."""
        if pipeline_state["running"]:
            print("[scheduler] pipeline 已经在跑, 跳过本次")
            return
        threading.Thread(
            target=_run_pipeline_bg, args=(False, None, None),
            daemon=True, name="ScheduledPipeline",
        ).start()

    settings_path = config.path("resume_dir").parent / "settings.json"
    scheduler = AgentScheduler(settings_path, _scheduled_run)
    scheduler.start()

    def render(name: str, **ctx) -> HTMLResponse:
        tpl = env.get_template(name)
        return HTMLResponse(tpl.render(**ctx))

    def _get_job(job_id: int) -> Job:
        with session_scope(db_path) as session:
            job = session.get(Job, job_id)
            if not job:
                raise HTTPException(404, f"Job {job_id} 不存在")
            session.expunge(job)
            return job

    # ---- helpers for health/stats ----
    def _compute_health() -> dict:
        """liveness + readiness + 问题列表."""
        persistent = agent_state.get_state(config)
        cur = persistent.get("current_run")
        last = persistent.get("last_run")
        cron_info = cron_status()
        schedule_hours = cron_info["hours"] or scheduler.get_schedule_hours() or 0

        # Liveness
        issues_live: list[str] = []
        if cur:
            liveness = "running"
        elif schedule_hours == 0:
            liveness = "no-schedule"
            issues_live.append("没设置定时任务 (cron 或进程内)")
        elif last and last.get("ended_at"):
            try:
                last_end = datetime.fromisoformat(last["ended_at"])
                gap_h = (datetime.now() - last_end).total_seconds() / 3600
                if gap_h > schedule_hours * 2:
                    liveness = "stale"
                    issues_live.append(
                        f"上次跑 {gap_h:.1f}h 前, 超出预期 {schedule_hours}h × 2"
                    )
                else:
                    liveness = "healthy"
            except Exception:
                liveness = "unknown"
        else:
            liveness = "untested"
            issues_live.append("有定时任务但还从未跑过")

        # Readiness
        from .profile_analyzer import load_profile as _lp
        issues_ready: list[str] = []
        has_profile = _lp(config) is not None
        has_resume = len(list_resumes(config.path("resume_dir"))) > 0
        has_deepseek = bool(os.getenv("DEEPSEEK_API_KEY"))
        has_apify = bool(os.getenv("APIFY_API_TOKEN"))
        if not has_profile: issues_ready.append("无激活画像 (做 onboarding)")
        if not has_resume:  issues_ready.append("没有简历 (上传一份)")
        if not has_deepseek: issues_ready.append("DEEPSEEK_API_KEY 未设置")
        if not has_apify:   issues_ready.append("APIFY_API_TOKEN 未设置")
        readiness = "ready" if not issues_ready else "not-ready"

        return {
            "liveness": liveness,
            "readiness": readiness,
            "issues_live": issues_live,
            "issues_ready": issues_ready,
            "checks": {
                "has_profile": has_profile,
                "has_resume": has_resume,
                "has_deepseek_key": has_deepseek,
                "has_apify_token": has_apify,
                "schedule_hours": schedule_hours,
            },
        }

    def _compute_counts() -> dict:
        """累计任务计数."""
        from sqlalchemy import func
        week_ago = datetime.now() - timedelta(days=7)
        day_ago = datetime.now() - timedelta(days=1)

        with session_scope(db_path) as session:
            total = session.scalar(select(func.count(Job.id))) or 0
            scored = session.scalar(
                select(func.count(Job.id)).where(Job.match_score.is_not(None))
            ) or 0
            high = session.scalar(
                select(func.count(Job.id)).where(Job.match_score >= 75)
            ) or 0
            applied = session.scalar(
                select(func.count(Job.id)).where(Job.status == JobStatus.APPLIED.value)
            ) or 0
            tailored = session.scalar(
                select(func.count(Job.id)).where(Job.tailored_resume_pdf_path.is_not(None))
            ) or 0
            week = session.scalar(
                select(func.count(Job.id)).where(Job.created_at >= week_ago)
            ) or 0
            day = session.scalar(
                select(func.count(Job.id)).where(Job.created_at >= day_ago)
            ) or 0
        return {
            "total_jobs": total,
            "scored_jobs": scored,
            "high_match_jobs": high,
            "tailored_jobs": tailored,
            "applied_jobs": applied,
            "week_jobs": week,
            "day_jobs": day,
        }

    # ---- routes ----
    @app.get("/")
    def root():
        """根路径总是去 onboarding (历史 + 表单)."""
        return RedirectResponse(url="/onboarding", status_code=303)

    # ===== k8s 风格 health probes =====
    @app.get("/health/liveness")
    def liveness_probe():
        h = _compute_health()
        # 200 当 liveness 是 running/healthy, 否则 503
        status_code = 200 if h["liveness"] in ("running", "healthy") else 503
        return JSONResponse({
            "status": h["liveness"],
            "issues": h["issues_live"],
            "current_run": agent_state.get_state(config).get("current_run"),
        }, status_code=status_code)

    @app.get("/health/readiness")
    def readiness_probe():
        h = _compute_health()
        status_code = 200 if h["readiness"] == "ready" else 503
        return JSONResponse({
            "status": h["readiness"],
            "issues": h["issues_ready"],
            "checks": h["checks"],
        }, status_code=status_code)

    @app.get("/health")
    def health_combined():
        h = _compute_health()
        counts = _compute_counts()
        lifetime = agent_state.get_lifetime_stats(config)
        return JSONResponse({
            "liveness": h["liveness"],
            "readiness": h["readiness"],
            "issues_live": h["issues_live"],
            "issues_ready": h["issues_ready"],
            "checks": h["checks"],
            "counts": counts,
            "lifetime": lifetime,
        })

    @app.get("/applied")
    def applied_list(sort: str = "applied_at"):
        """已投递追踪页. 列出所有 status in (applied/interview/offer/rejected) 的岗位."""
        tracked_statuses = [
            JobStatus.APPLIED.value,
            JobStatus.INTERVIEW.value,
            JobStatus.OFFER.value,
            JobStatus.REJECTED.value,
            JobStatus.SHORTLISTED.value,
        ]
        with session_scope(db_path) as session:
            stmt = select(Job).where(Job.status.in_(tracked_statuses))
            jobs = list(session.scalars(stmt).all())
            for j in jobs:
                session.expunge(j)

        # 默认按 applied_at 降序 (没投的放最后)
        if sort == "applied_at":
            jobs.sort(key=lambda j: -(j.applied_at.timestamp() if j.applied_at else 0))
        elif sort == "score":
            jobs.sort(key=lambda j: -(j.match_score or 0))
        elif sort == "status":
            order = {"offer": 0, "interview": 1, "applied": 2, "shortlisted": 3, "rejected": 4}
            jobs.sort(key=lambda j: order.get(j.status, 99))

        return render(
            "applied.html",
            jobs=jobs,
            sort=sort,
            total=len(jobs),
        )

    @app.post("/job/{job_id}/status")
    def update_status(job_id: int, to: str = Form(...)):
        """通用状态切换. to in {shortlisted, applied, interview, offer, rejected, archived}"""
        allowed = {s.value for s in JobStatus}
        if to not in allowed:
            raise HTTPException(400, f"非法状态 {to}, 允许: {sorted(allowed)}")
        with session_scope(db_path) as session:
            job = session.get(Job, job_id)
            if not job:
                raise HTTPException(404, "Job 不存在")
            job.status = to
            if to == JobStatus.APPLIED.value and not job.applied_at:
                job.applied_at = datetime.utcnow()
            session.add(job)
            session.commit()
        return RedirectResponse(url=f"/job/{job_id}", status_code=303)

    @app.get("/jobs")
    def jobs_list(
        min_score: float = 70.0,
        status: str = "active",
        sort: str = "score",
        profile_id: int | None = None,
    ):
        with session_scope(db_path) as session:
            stmt = select(Job)
            if status == "active":
                stmt = stmt.where(Job.status != JobStatus.ARCHIVED.value)
            elif status != "all":
                stmt = stmt.where(Job.status == status)
            if min_score is not None:
                stmt = stmt.where(Job.match_score >= min_score)
            if profile_id is not None:
                stmt = stmt.where(Job.profile_id == profile_id)
            jobs = list(session.scalars(stmt).all())
            for j in jobs:
                session.expunge(j)

            # 当前过滤 profile 的 label, 用于显示横幅
            profile_label = None
            if profile_id is not None:
                from .db import Profile
                p = session.get(Profile, profile_id)
                profile_label = p.label if p else f"#{profile_id} (已删除)"

        sort_fn = SORTERS.get(sort, SORTERS["score"])
        jobs.sort(key=sort_fn)

        return render(
            "index.html",
            jobs=jobs,
            min_score=min_score,
            status=status,
            sort=sort,
            total=len(jobs),
            profile_id=profile_id,
            profile_label=profile_label,
        )

    @app.get("/job/{job_id}")
    def job_detail(job_id: int):
        job = _get_job(job_id)
        return render(
            "job_detail.html",
            job=job,
            strengths_list=(job.match_strengths or "").splitlines(),
            fit_bullets=(job.match_fit_bullets or "").splitlines(),
            keywords_list=(job.match_keywords or "").splitlines(),
        )

    @app.post("/job/{job_id}/tailor")
    def trigger_tailor(job_id: int, with_cover: bool = True, name: str = "Yuanjun Dai"):
        try:
            resume_text = load_cached(config.path("resume_dir"))
        except FileNotFoundError as e:
            raise HTTPException(400, f"找不到简历: {e}")

        try:
            tailor_for_job(config, resume_text, job_id, candidate_name=name)
            if with_cover:
                write_cover_letter(config, resume_text, job_id)
        except Exception as e:
            raise HTTPException(500, f"tailor 失败: {e}")

        return RedirectResponse(url=f"/job/{job_id}", status_code=303)

    @app.get("/job/{job_id}/pdf")
    def job_pdf(job_id: int):
        job = _get_job(job_id)
        if not job.tailored_resume_pdf_path:
            raise HTTPException(404, "PDF 还没生成,先点 Tailor")
        path = Path(job.tailored_resume_pdf_path)
        if not path.exists():
            raise HTTPException(404, f"PDF 文件不存在: {path}")
        return FileResponse(str(path), media_type="application/pdf", filename=path.name)

    @app.get("/job/{job_id}/md")
    def job_md(job_id: int):
        job = _get_job(job_id)
        if not job.tailored_resume_path:
            raise HTTPException(404, "MD 还没生成,先点 Tailor")
        path = Path(job.tailored_resume_path)
        if not path.exists():
            raise HTTPException(404, f"MD 文件不存在: {path}")
        return FileResponse(str(path), media_type="text/markdown", filename=path.name)

    @app.get("/job/{job_id}/cover")
    def job_cover(job_id: int):
        job = _get_job(job_id)
        if not job.cover_letter_path:
            raise HTTPException(404, "Cover letter 还没生成")
        path = Path(job.cover_letter_path)
        return FileResponse(str(path), media_type="text/plain", filename=path.name)

    @app.post("/job/{job_id}/mark-applied")
    def mark_app(job_id: int, note: Optional[str] = None):
        mark_applied(config, job_id, note=note)
        return RedirectResponse(url=f"/job/{job_id}", status_code=303)

    # ---- Onboarding (首次使用 / 重设画像) ----
    @app.get("/onboarding")
    def onboarding_form():
        existing_desc = load_user_description(config) or ""
        profile = load_profile(config)

        # 老 JSON 自动迁移到 DB (只跑一次): 如果有 _profile.json 但 DB 里没对应 row,
        # 就插一行历史. 之后历史 table 就有内容了.
        if profile and not get_current_profile_id(config):
            save_profile_snapshot(
                config, profile,
                user_description=existing_desc or "(从旧 _profile.json 自动导入)",
                resume_filename=None,
            )

        history = list_profile_snapshots(config)
        current_id = get_current_profile_id(config)

        # 每个 profile 的岗位计数 (展示在历史行)
        from sqlalchemy import func
        job_counts: dict[int, dict] = {}
        with session_scope(db_path) as session:
            for h in history:
                total = session.scalar(
                    select(func.count(Job.id)).where(Job.profile_id == h.id)
                ) or 0
                top = session.scalar(
                    select(func.count(Job.id)).where(
                        Job.profile_id == h.id, Job.match_score >= 75
                    )
                ) or 0
                job_counts[h.id] = {"total": total, "top": top}

        # 所有上传过的简历, mtime 降序 (第一个是当前激活的)
        resume_dir = config.path("resume_dir")
        resume_files: list[dict[str, Any]] = []
        for p in list_resumes(resume_dir):
            st = p.stat()
            resume_files.append({
                "filename": p.name,
                "size_kb": round(st.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "paused": False,
            })
        for p in list_paused_resumes(resume_dir):
            st = p.stat()
            resume_files.append({
                "filename": p.name,
                "size_kb": round(st.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "paused": True,
            })

        cron_info = cron_status()
        schedule_info = {
            "inproc_hours": scheduler.get_schedule_hours(),
            "last": scheduler.get_last_run(),
            "next": scheduler.get_next_run(),
            "cron_available": cron_info["available"],
            "cron_installed": cron_info["installed"],
            "cron_hours": cron_info["hours"],
            "cron_line": cron_info["line"],
        }
        freshness_info = {"hours": int(config.freshness.get("max_age_hours", 24))}

        # Agent 总体状态
        has_profile = profile is not None
        has_schedule = cron_info["installed"] or scheduler.get_schedule_hours() > 0
        # 持久化状态 (含 cron 启动的进程)
        persistent = agent_state.get_state(config)
        cur_run = persistent.get("current_run")
        last_run = persistent.get("last_run")
        is_running = pipeline_state["running"] or (cur_run is not None)
        if not has_profile:
            agent_status = "uninitialized"
        elif is_running:
            agent_status = "running"
        elif has_schedule:
            agent_status = "scheduled"
        else:
            agent_status = "idle"

        # 计算 elapsed
        cur_elapsed = ""
        if cur_run and cur_run.get("started_at"):
            try:
                start = datetime.fromisoformat(cur_run["started_at"])
                secs = int((datetime.now() - start).total_seconds())
                cur_elapsed = f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60}s"
            except Exception:
                pass

        return render(
            "onboarding.html",
            existing_desc=existing_desc,
            has_profile=profile is not None,
            supported_exts=", ".join(sorted(SUPPORTED_EXTS)),
            history=history,
            current_id=current_id,
            job_counts=job_counts,
            pipeline_running=pipeline_state["running"],
            resume_files=resume_files,
            schedule=schedule_info,
            freshness=freshness_info,
            agent_state=agent_status,
            persistent_state=persistent,
            cur_run=cur_run,
            last_run=last_run,
            cur_elapsed=cur_elapsed,
            health=_compute_health(),
            counts=_compute_counts(),
            lifetime=agent_state.get_lifetime_stats(config),
        )

    @app.post("/onboarding/submit")
    async def onboarding_submit(
        background_tasks: BackgroundTasks,
        description: str = Form(...),
        resume: UploadFile | None = File(None),
    ):
        # 如果当前描述跟上次完全相同, 不重复跑
        existing = load_user_description(config) or ""
        desc = (description or "").strip()
        if not desc:
            raise HTTPException(400, "请填写求职需求描述")

        if existing.strip() == desc and not (resume and resume.filename):
            # 描述和简历都没变, 直接回主页
            return RedirectResponse(url="/", status_code=303)

        # 如果有流水线在跑, 先中断
        if pipeline_state["running"]:
            print("[onboarding] 检测到流水线在跑, 请求取消...")
            _wait_for_cancel(timeout=60)

        # 1) 如果有新简历, 保存
        resume_dir = config.path("resume_dir")
        uploaded_filename = None
        if resume and resume.filename:
            ext = Path(resume.filename).suffix.lower()
            if ext not in SUPPORTED_EXTS:
                raise HTTPException(
                    400,
                    f"不支持的简历格式 {ext}. 仅支持: {sorted(SUPPORTED_EXTS)}",
                )
            content = await resume.read()
            if not content:
                raise HTTPException(400, "上传文件为空")
            target = resume_dir / resume.filename
            target.write_bytes(content)
            uploaded_filename = resume.filename
            try:
                parse_and_cache(resume_dir)
            except Exception as e:
                raise HTTPException(500, f"解析简历失败: {e}")

        # 2) 保存用户描述
        save_user_description(config, desc)

        # 3) 确认 DB 在
        init_db(config.path("db_path"))

        # 4) 不再阻塞: analyze + collect + match 都丢后台
        background_tasks.add_task(_run_pipeline_bg, True, desc, uploaded_filename)

        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.get("/profiles/{profile_id}")
    def profile_detail(profile_id: int):
        """单个历史画像详情: 完整描述 + Top-5 + 区域公司 + 本画像跑出的岗位."""
        from .db import Profile
        from .profile_analyzer import ProfileAnalysis
        import json as _json
        with session_scope(db_path) as session:
            row = session.get(Profile, profile_id)
            if not row:
                raise HTTPException(404, f"Profile #{profile_id} 不存在")
            try:
                pa = ProfileAnalysis.from_dict(_json.loads(row.profile_json))
            except Exception:
                pa = None

            # 这个画像跑出来的所有岗位
            stmt = select(Job).where(Job.profile_id == profile_id).order_by(Job.match_score.desc().nulls_last())
            jobs = list(session.scalars(stmt).all())
            for j in jobs:
                session.expunge(j)
            session.expunge(row)

        current_id = get_current_profile_id(config)
        return render(
            "profile_detail.html",
            profile=row,
            analysis=pa,
            jobs=jobs,
            is_current=(row.id == current_id),
            pipeline_running=pipeline_state["running"],
        )

    def _safe_resume_path(filename: str) -> Path:
        resume_dir = config.path("resume_dir")
        target = (resume_dir / filename).resolve()
        # 防 path traversal
        if resume_dir.resolve() not in target.parents and target.parent != resume_dir.resolve():
            raise HTTPException(400, "非法文件名")
        if not target.exists():
            raise HTTPException(404, f"简历不存在: {filename}")
        if target.suffix.lower() not in SUPPORTED_EXTS:
            raise HTTPException(400, "不支持的格式")
        return target

    @app.post("/resume/{filename}/activate")
    def activate_resume(filename: str, background_tasks: BackgroundTasks):
        """激活某份简历: touch + 重新 parse + 用当前画像跑一遍流水线 (collect + match)."""
        target = _safe_resume_path(filename)
        target.touch()
        try:
            parse_and_cache(config.path("resume_dir"))
        except Exception as e:
            raise HTTPException(500, f"重新解析失败: {e}")
        # 取消正在跑的(如有), 然后启动新流水线
        if pipeline_state["running"]:
            _wait_for_cancel(timeout=60)
        background_tasks.add_task(_run_pipeline_bg, False, None, None)
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.post("/resume/{filename}/delete")
    def delete_resume(filename: str):
        # 先尝试在主目录, 找不到再尝试 _paused/
        resume_dir = config.path("resume_dir")
        active_target = resume_dir / filename
        if active_target.exists() and active_target.is_file():
            active_target.unlink()
        else:
            delete_paused_resume(resume_dir, filename)
        # 还有其他活跃简历就刷 cache; 否则清空
        if list_resumes(resume_dir):
            try:
                parse_and_cache(resume_dir)
            except Exception:
                pass
        else:
            cache = resume_dir / "_parsed.txt"
            if cache.exists():
                cache.unlink()
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/resume/{filename}/pause")
    def pause_resume(filename: str):
        resume_dir = config.path("resume_dir")
        try:
            pause_resume_file(resume_dir, filename)
        except FileNotFoundError:
            raise HTTPException(404, f"简历不存在: {filename}")
        # 暂停了当前激活的话, 下个最新非暂停的成为激活, 刷新 cache
        if list_resumes(resume_dir):
            try:
                parse_and_cache(resume_dir)
            except Exception:
                pass
        else:
            cache = resume_dir / "_parsed.txt"
            if cache.exists():
                cache.unlink()
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/resume/{filename}/unpause")
    def unpause_resume(filename: str):
        resume_dir = config.path("resume_dir")
        try:
            unpause_resume_file(resume_dir, filename)
        except FileNotFoundError:
            raise HTTPException(404, f"暂停目录里没有: {filename}")
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        # 恢复完, 刷 cache (它会成为最新, 也就是新的激活)
        try:
            parse_and_cache(resume_dir)
        except Exception:
            pass
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/resume/upload")
    async def upload_resume_only(resume: UploadFile = File(...)):
        """单独上传/替换简历, 不触发流水线. 后续任何 run-all / refresh 都用最新的."""
        if not resume.filename:
            raise HTTPException(400, "未选择文件")
        ext = Path(resume.filename).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            raise HTTPException(400, f"不支持的格式 {ext}. 仅支持: {sorted(SUPPORTED_EXTS)}")
        content = await resume.read()
        if not content:
            raise HTTPException(400, "上传文件为空")
        resume_dir = config.path("resume_dir")
        target = resume_dir / resume.filename
        target.write_bytes(content)
        # 强制重新解析,刷新 _parsed.txt 缓存
        try:
            parse_and_cache(resume_dir)
        except Exception as e:
            raise HTTPException(500, f"解析失败: {e}")
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/profiles/{profile_id}/use")
    def use_profile(profile_id: int, background_tasks: BackgroundTasks):
        """切回某历史画像 + 重跑流水线."""
        # 取消当前跑的(如有)
        if pipeline_state["running"]:
            print(f"[use_profile] 流水线在跑, 请求取消以切换到 #{profile_id}...")
            _wait_for_cancel(timeout=60)
        try:
            activate_profile_snapshot(config, profile_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        # do_analyze=False: 直接用已激活的画像跑 collect+match
        background_tasks.add_task(_run_pipeline_bg, False, None, None)
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.post("/schedule/set")
    def set_schedule(hours: int = Form(...), backend: str = Form("cron")):
        """设置自动跑.

        backend='cron'   : 写入系统 crontab (24/7 真后台)
        backend='inproc' : 进程内调度 (只在 web server 运行时跑)
        """
        if backend == "cron":
            script = Path(__file__).resolve().parent.parent / "scripts" / "daily.sh"
            try:
                if hours == 0:
                    cron_uninstall()
                else:
                    cron_install(hours, script)
                # 同时关闭 in-process 调度避免双触发
                scheduler.set_schedule_hours(0)
            except Exception as e:
                raise HTTPException(500, f"系统 cron 操作失败: {e}")
        else:
            # 进程内调度
            scheduler.set_schedule_hours(hours)
            # 关闭 cron 避免双触发
            try:
                cron_uninstall()
            except Exception:
                pass
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/freshness/set")
    def set_freshness(hours: int = Form(...)):
        """设置抓取时间窗 (config.freshness 会自动读这个值)."""
        import json as _json
        settings_path = config.path("resume_dir").parent / "settings.json"
        try:
            data = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
        except Exception:
            data = {}
        data["freshness_hours"] = max(1, int(hours))
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            _json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/pipeline/cancel")
    def cancel_pipeline():
        if not pipeline_state["running"]:
            return RedirectResponse(url="/onboarding/processing", status_code=303)
        pipeline_state["cancel_requested"] = True
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    # ===== Agent 全局控制 =====
    @app.post("/agent/pause")
    def agent_pause():
        """暂停 agent: 取消正在跑的, 关 in-process 调度, 卸 cron. 不动数据."""
        # 1. 取消当前流水线
        if pipeline_state["running"]:
            pipeline_state["cancel_requested"] = True
        # 2. 关进程内调度
        scheduler.set_schedule_hours(0)
        # 3. 卸系统 cron
        try:
            cron_uninstall()
        except Exception:
            pass
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/agent/delete")
    def agent_delete():
        """删除 agent: 暂停 + 清画像 + 解绑所有岗位 profile_id, 回到 onboarding 初态.

        不删 Jobs 数据 (你历史的岗位还在 /jobs 看得到), 不删简历文件.
        """
        # 1. 暂停
        if pipeline_state["running"]:
            pipeline_state["cancel_requested"] = True
            _wait_for_cancel(timeout=30)
        scheduler.set_schedule_hours(0)
        try:
            cron_uninstall()
        except Exception:
            pass

        # 2. 清画像
        resume_dir = config.path("resume_dir")
        for fname in ("_profile.json", "_user_description.txt"):
            f = resume_dir / fname
            if f.exists():
                f.unlink()
        # DB 里所有 profile 取消激活 (但不删历史快照, 用户可以再激活)
        from sqlalchemy import update as _update
        from .db import Profile
        with session_scope(db_path) as session:
            session.execute(_update(Profile).where(Profile.is_current).values(is_current=False))
            session.commit()

        # 3. 清 pipeline 状态 (内存 + 持久化)
        pipeline_state.update(
            running=False,
            phase="idle",
            started_at=None,
            ended_at=None,
            error=None,
            stats={},
            profile_id=None,
            cancel_requested=False,
            current_platform=None,
            platform_started_at=None,
        )
        try:
            agent_state.clear(config)
        except Exception:
            pass
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/profiles/{profile_id}/delete")
    def delete_profile(profile_id: int):
        """删除任意画像. 如果删的是当前激活的, 自动激活下一个最新的画像;
        没有其他画像则清空 _profile.json + _user_description.txt, 用户需重新 onboarding.
        """
        from .db import Profile
        from sqlalchemy import update as _update
        was_current = False
        with session_scope(db_path) as session:
            row = session.get(Profile, profile_id)
            if not row:
                raise HTTPException(404, "画像不存在")
            was_current = bool(row.is_current)
            # 解绑该画像下的所有 jobs
            session.execute(
                _update(Job).where(Job.profile_id == profile_id).values(profile_id=None)
            )
            session.delete(row)
            session.commit()

        # 如果删的是 current,激活次新的;没有就清空 JSON
        if was_current:
            with session_scope(db_path) as session:
                next_row = session.scalar(
                    select(Profile).order_by(Profile.created_at.desc()).limit(1)
                )
                if next_row:
                    next_id = next_row.id
                else:
                    next_id = None

            if next_id is not None:
                try:
                    activate_profile_snapshot(config, next_id)
                except Exception as e:
                    print(f"[delete] 自动激活次新画像失败: {e}")
            else:
                # 没有别的画像了, 清空 JSON + 用户描述, 回到无 profile 状态
                resume_dir = config.path("resume_dir")
                for fname in ("_profile.json", "_user_description.txt"):
                    f = resume_dir / fname
                    if f.exists():
                        f.unlink()

        return RedirectResponse(url="/onboarding", status_code=303)

    @app.get("/onboarding/processing")
    def onboarding_processing():
        plat_elapsed = ""
        if pipeline_state.get("platform_started_at"):
            secs = int((datetime.now() - pipeline_state["platform_started_at"]).total_seconds())
            plat_elapsed = f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60}s"
        return render(
            "processing.html",
            state=pipeline_state,
            elapsed=_elapsed(pipeline_state),
            platform_elapsed=plat_elapsed,
        )

    @app.post("/refresh")
    def refresh(background_tasks: BackgroundTasks):
        """主页点 'Refresh' 时手动重跑 collect + match (不重做 analyze)."""
        if pipeline_state["running"]:
            raise HTTPException(409, "已有流水线在跑")
        background_tasks.add_task(_run_pipeline_bg, False, None)
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    return app


def _elapsed(state: dict) -> str:
    if not state.get("started_at"):
        return ""
    end = state.get("ended_at") or datetime.now()
    delta = end - state["started_at"]
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


def _find_free_port(host: str, start: int, max_tries: int = 20) -> int:
    """从 start 开始递增找一个能 bind 上的端口."""
    import socket
    for p in range(start, start + max_tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, p))
            s.close()
            return p
        except OSError:
            s.close()
            continue
    raise RuntimeError(f"在 {start}..{start + max_tries - 1} 范围内找不到可用端口")


def run_server(config: Config, host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    actual_port = _find_free_port(host, port)
    if actual_port != port:
        print(f"⚠️  端口 {port} 被占用,改用 {actual_port}")
    app = create_app(config)
    print(f"\n→ JobHunter web UI: http://{host}:{actual_port}\n")
    uvicorn.run(app, host=host, port=actual_port, log_level="info")
