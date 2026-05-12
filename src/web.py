"""Local web UI: view jobs + click to trigger Claude resume customization + PDF.

Launch: `python -m src.main web` -> http://127.0.0.1:8765

To avoid fastapi/starlette/jinja2 version conflicts, this version **does not use Jinja2Templates**,
instead directly renders Jinja2 strings and returns HTMLResponse.
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


# ========= Sorting helpers =========
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
    """Extract minimum salary number (USD) from strings like '$120,000 - $150,000' / '80K - 110K USD'.

    Used for sorting; returns 0 if unable to extract.
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
        if m.group(2):  # K/k suffix
            n *= 1000
        elif n < 1000:  # Pure number < 1000 (e.g., "120") treated as K
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


def _render_md_simple(md: str) -> str:
    """Lightweight markdown → HTML for Jinja2 filter use (resume preview)."""
    import re as _re
    if not md:
        return ""
    h = (md
         .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
         .replace("**", "\x00")  # temp marker for bold
    )
    # bold (between markers)
    parts = h.split("\x00")
    h = "".join(f"<strong>{p}</strong>" if i % 2 else p for i, p in enumerate(parts))
    # links
    h = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank">\1</a>', h)
    # headings
    h = _re.sub(r"^### (.+)$", r"<h3>\1</h3>", h, flags=_re.MULTILINE)
    h = _re.sub(r"^## (.+)$", r"<h2>\1</h2>", h, flags=_re.MULTILINE)
    h = _re.sub(r"^# (.+)$", r"<h1>\1</h1>", h, flags=_re.MULTILINE)
    # bullets
    h = _re.sub(r"^- (.+)$", r"<li>\1</li>", h, flags=_re.MULTILINE)
    h = _re.sub(r"(<li>.*?</li>\n?)+", lambda m: "<ul>" + m.group(0) + "</ul>", h, flags=_re.DOTALL)
    # blockquote
    h = _re.sub(r"^> (.+)$", r"<blockquote>\1</blockquote>", h, flags=_re.MULTILINE)
    # paragraphs
    h = _re.sub(r"\n{2,}", "<br><br>", h)
    return h


def _make_env() -> Environment:
    """Construct Jinja2 Environment directly, not through starlette wrapper, avoid cache_key bug."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        cache_size=0,
    )
    from markupsafe import Markup
    env.filters["render_md"] = lambda md: Markup(_render_md_simple(md or ""))
    return env


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="JobHunter")
    env = _make_env()
    db_path = config.path("db_path")
    # Ensure all tables and migrations are in place at startup
    init_db(db_path)

    # ===== Background pipeline state (single instance sufficient, multi-user needs DB) =====
    pipeline_state: dict[str, Any] = {
        "running": False,
        "phase": "idle",            # idle | analyzing | collecting | matching | done | cancelled | error
        "started_at": None,
        "ended_at": None,
        "error": None,
        "stats": {},
        "profile_id": None,         # Current pipeline's bound profile id
        "cancel_requested": False,
        "current_platform": None,   # Which platform is running during collection phase
        "platform_started_at": None,
    }
    pipeline_lock = threading.Lock()

    def _should_continue():
        return not pipeline_state["cancel_requested"]

    def _run_pipeline_bg(
        do_analyze: bool,
        user_description: str | None = None,
        resume_filename: str | None = None,
        job_types: list[str] | None = None,
    ):
        """Run [analyze_profile] -> collect -> match in background. Graceful exit on failure."""
        with pipeline_lock:
            if pipeline_state["running"]:
                return  # Prevent re-entrance
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

        # Sync write persistent state (visible to other processes/web sessions)
        try:
            agent_state.start_run(config, trigger="web")
            agent_state.update_phase(config, pipeline_state["phase"])
        except Exception:
            pass

        try:
            if do_analyze:
                profile = analyze_profile(
                    config,
                    user_description=user_description,
                    job_types=job_types or ["Full-time"],
                )
                save_profile(config, profile)
                # Also write to history DB
                pid = save_profile_snapshot(
                    config, profile,
                    user_description=user_description or "",
                    resume_filename=resume_filename,
                )
                pipeline_state["profile_id"] = pid
                # Sync current profile info to persistent state
                try:
                    agent_state.update_phase(
                        config, pipeline_state["phase"],
                        profile_id=pid,
                        profile_label=profile.summary[:80] if profile.summary else None,
                    )
                except Exception:
                    pass
            else:
                # Use existing current profile id
                pipeline_state["profile_id"] = get_current_profile_id(config)
                # Get label
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

            # Resolve job_types: prefer explicit param, then current profile's setting, then config default
            _collect_job_types = job_types  # from caller (do_analyze=True path)
            if not _collect_job_types and pipeline_state["profile_id"]:
                try:
                    from .db import Profile as _P
                    with session_scope(db_path) as _s:
                        _row = _s.get(_P, pipeline_state["profile_id"])
                        if _row and _row.job_types_json:
                            _collect_job_types = _json.loads(_row.job_types_json)
                except Exception:
                    pass
            if not _collect_job_types:
                _collect_job_types = config.preferences.get("job_types") or ["Full-time"]

            stats = collect_all(
                config,
                should_continue=_should_continue,
                profile_id=pipeline_state["profile_id"],
                on_platform_start=_on_platform,
                job_types=_collect_job_types,
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
            # Write final persistent state
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
        """Request cancellation, poll until pipeline actually stops (or timeout). Return success."""
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

    # ===== Automatic scheduling =====
    def _scheduled_run(run_trends: bool = False):
        """Scheduler callback: collect + match + digest [+ trends(weekly)].

        Full workflow:
          1. collect: scrape all new jobs into database (no limit)
          2. match:   score all jobs with NEW status
          3. digest:  send top-15 high-scoring jobs email
          4. trends:  (weekly) market trend analysis email
        """
        if pipeline_state["running"]:
            print("[scheduler] pipeline already running, skip this cycle")
            return

        from .multiagent import JobAgentRunOptions, run_job_agent_graph
        from .profile_analyzer import load_profile as _lp

        profile_id = get_current_profile_id(config)
        profile_label = None
        if profile_id:
            with session_scope(db_path) as _s:
                from .db import Profile as _P
                row = _s.get(_P, profile_id)
                if row:
                    profile_label = row.label

        def _bg():
            with pipeline_lock:
                if pipeline_state["running"]:
                    return
                pipeline_state.update(running=True, phase="collecting",
                                      started_at=datetime.now(), ended_at=None,
                                      error=None, stats={}, cancel_requested=False,
                                      profile_id=profile_id)
            try:
                from .collect import PLATFORMS as _ALL_PLATS
                options = JobAgentRunOptions(
                    platforms=_ALL_PLATS,
                    trigger="scheduler",
                    collect=True,
                    digest=True,
                    trends=run_trends,
                    profile_id=profile_id,
                    profile_label=profile_label,
                )
                run_job_agent_graph(config, options)
                with pipeline_lock:
                    pipeline_state.update(running=False, phase="done", ended_at=datetime.now())
                # Mark run time after completion, whether triggered by scheduler or manual
                # Prevent scheduler from repeating if last_auto_run=None on next cycle (60s)
                scheduler.mark_ran(include_trends=run_trends)
            except Exception as e:
                with pipeline_lock:
                    pipeline_state.update(running=False, phase="error", error=str(e), ended_at=datetime.now())
                scheduler.mark_ran(include_trends=False)  # Mark even on failure to avoid infinite retry
                print(f"[scheduler] pipeline failed: {e}")

        threading.Thread(target=_bg, daemon=True, name="ScheduledPipeline").start()

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
                raise HTTPException(404, f"Job {job_id} does not exist")
            session.expunge(job)
            return job

    # ---- helpers for health/stats ----
    def _agent_readiness(agent_name: str, checks: dict) -> dict:
        """Per-agent readiness: whether prerequisite conditions are met."""
        req: dict[str, list[str]] = {
            "context_agent":    ["has_resume"],
            "collection_agent": ["has_profile", "has_apify_token"],
            "matching_agent":   ["has_resume", "has_deepseek_key"],
            "digest_agent":     [],
            "trend_agent":      [],
            "validation_agent": [],
        }
        missing = [k for k in req.get(agent_name, []) if not checks.get(k)]
        return {"ready": not missing, "missing": missing}

    def _compute_health() -> dict:
        """liveness + readiness + issue list."""
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
            issues_live.append("No scheduled task configured (cron or in-process)")
        elif last and last.get("ended_at"):
            try:
                last_end = datetime.fromisoformat(last["ended_at"])
                gap_h = (datetime.now() - last_end).total_seconds() / 3600
                if gap_h > schedule_hours * 2:
                    liveness = "stale"
                    issues_live.append(
                        f"Last run {gap_h:.1f}h ago, exceeds expected {schedule_hours}h × 2"
                    )
                else:
                    liveness = "healthy"
            except Exception:
                liveness = "unknown"
        else:
            liveness = "untested"
            issues_live.append("Has scheduled task but never run")

        # Readiness
        from .profile_analyzer import load_profile as _lp
        issues_ready: list[str] = []
        has_profile = _lp(config) is not None
        has_resume = len(list_resumes(config.path("resume_dir"))) > 0
        has_deepseek = bool(os.getenv("DEEPSEEK_API_KEY"))
        has_apify = bool(os.getenv("APIFY_API_TOKEN"))
        if not has_profile: issues_ready.append("No active profile (run onboarding)")
        if not has_resume:  issues_ready.append("No resume (upload one)")
        if not has_deepseek: issues_ready.append("DEEPSEEK_API_KEY not set")
        if not has_apify:   issues_ready.append("APIFY_API_TOKEN not set")
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
        """Cumulative task counts."""
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
        """Root path always goes to onboarding (history + form)."""
        return RedirectResponse(url="/onboarding", status_code=303)

    # ===== k8s-style health probes =====
    @app.get("/health/liveness")
    def liveness_probe():
        h = _compute_health()
        agents_live = agent_state.get_agents_liveness(config)
        # 503 if pipeline liveness is bad OR any agent is stuck
        ok = h["liveness"] in ("running", "healthy") and not agents_live["any_stuck"]
        status_code = 200 if ok else 503
        return JSONResponse({
            "status": h["liveness"],
            "issues": h["issues_live"],
            "current_run": agent_state.get_state(config).get("current_run"),
            "agents": {
                name: {
                    "status": a["status"],
                    "stuck": a.get("stuck", False),
                    "heartbeat_age_sec": a.get("heartbeat_age_sec"),
                    "heartbeat_at": a.get("heartbeat_at"),
                }
                for name, a in agents_live["agents"].items()
            },
            "stuck_agents": agents_live["stuck_agents"],
        }, status_code=status_code)

    @app.get("/health/readiness")
    def readiness_probe():
        h = _compute_health()
        status_code = 200 if h["readiness"] == "ready" else 503
        return JSONResponse({
            "status": h["readiness"],
            "issues": h["issues_ready"],
            "checks": h["checks"],
            "agents_readiness": {
                name: _agent_readiness(name, h["checks"])
                for name in agent_state.ALL_AGENTS
            },
        }, status_code=status_code)

    @app.get("/health/agents")
    def health_agents():
        """Per-agent granular status: status, heartbeat, stuck, duration, meta."""
        agents = agent_state.get_agents_state(config)
        timeouts = agent_state.AGENT_HEARTBEAT_TIMEOUT
        return JSONResponse({
            name: {
                **info,
                "heartbeat_timeout_sec": timeouts.get(name, agent_state.DEFAULT_HEARTBEAT_TIMEOUT),
            }
            for name, info in agents.items()
        })

    @app.get("/health")
    def health_combined():
        h = _compute_health()
        counts = _compute_counts()
        lifetime = agent_state.get_lifetime_stats(config)
        agents_live = agent_state.get_agents_liveness(config)
        return JSONResponse({
            "liveness": h["liveness"],
            "readiness": h["readiness"],
            "issues_live": h["issues_live"],
            "issues_ready": h["issues_ready"],
            "checks": h["checks"],
            "counts": counts,
            "lifetime": lifetime,
            "agents": {
                name: {
                    "status": a["status"],
                    "stuck": a.get("stuck", False),
                    "duration_sec": a.get("duration_sec"),
                    "heartbeat_at": a.get("heartbeat_at"),
                    "error": a.get("error"),
                }
                for name, a in agents_live["agents"].items()
            },
            "stuck_agents": agents_live["stuck_agents"],
            "errored_agents": agents_live["errored_agents"],
        })

    # ---- Interview stage metadata (reused in multiple places) ----
    STAGE_META: dict[str, dict] = {
        "applied":        {"label_zh": "Applied",     "label_en": "Applied",        "color": "#4f46e5", "bg": "#eef2ff"},
        "phone_screen":   {"label_zh": "Phone Screen",   "label_en": "Phone Screen",   "color": "#0891b2", "bg": "#e0f2fe"},
        "hr_interview":   {"label_zh": "HR Interview",    "label_en": "HR Interview",   "color": "#7c3aed", "bg": "#f3e8ff"},
        "interview":      {"label_zh": "HR Interview",    "label_en": "HR Interview",   "color": "#7c3aed", "bg": "#f3e8ff"},  # Old data compatibility
        "hm_interview":   {"label_zh": "HM Interview",    "label_en": "HM Interview",   "color": "#d97706", "bg": "#fef3c7"},
        "final_round":    {"label_zh": "Final Round",        "label_en": "Final Round",    "color": "#ea580c", "bg": "#fff7ed"},
        "offer":          {"label_zh": "Offer",       "label_en": "Offer",          "color": "#16a34a", "bg": "#dcfce7"},
        "rejected":       {"label_zh": "Rejected",        "label_en": "Rejected",       "color": "#dc2626", "bg": "#fee2e2"},
        "shortlisted":    {"label_zh": "Shortlisted",        "label_en": "Shortlisted",    "color": "#ca8a04", "bg": "#fef9c3"},
    }

    INTERVIEW_STATUSES = [
        "applied", "phone_screen", "hr_interview", "interview",
        "hm_interview", "final_round", "offer", "rejected", "shortlisted",
    ]

    @app.get("/applied")
    def applied_list(sort: str = "applied_at"):
        """Applied applications tracking page."""
        with session_scope(db_path) as session:
            stmt = select(Job).where(Job.status.in_(INTERVIEW_STATUSES))
            jobs = list(session.scalars(stmt).all())
            for j in jobs:
                session.expunge(j)

        if sort == "applied_at":
            jobs.sort(key=lambda j: -(j.applied_at.timestamp() if j.applied_at else 0))
        elif sort == "score":
            jobs.sort(key=lambda j: -(j.match_score or 0))
        elif sort == "status":
            stage_order = {s: i for i, s in enumerate(["offer","final_round","hm_interview","hr_interview","interview","phone_screen","applied","shortlisted","rejected"])}
            jobs.sort(key=lambda j: stage_order.get(j.status, 99))

        return render(
            "applied.html",
            jobs=jobs,
            sort=sort,
            total=len(jobs),
            stage_meta=STAGE_META,
        )

    @app.get("/stats")
    def stats_page():
        """Job search statistics dashboard."""
        from sqlalchemy import func

        with session_scope(db_path) as session:
            # All applied jobs
            all_applied = list(session.scalars(
                select(Job).where(Job.status.in_(INTERVIEW_STATUSES))
                .order_by(Job.applied_at.desc().nulls_last(), Job.updated_at.desc())
            ).all())
            for j in all_applied:
                session.expunge(j)

            # Source distribution (only count applied jobs)
            src_rows = session.execute(
                select(Job.source, func.count(Job.id))
                .where(Job.status.in_(INTERVIEW_STATUSES))
                .group_by(Job.source)
                .order_by(func.count(Job.id).desc())
            ).all()

        # ---- Basic counts ----
        applied_total = len(all_applied)
        interview_statuses = {"phone_screen","hr_interview","interview","hm_interview","final_round"}
        in_process = [j for j in all_applied if j.status in interview_statuses]
        offers = [j for j in all_applied if j.status == "offer"]
        rejections = [j for j in all_applied if j.status == "rejected"]
        got_reply = [j for j in all_applied if j.status not in {"applied","shortlisted"}]
        response_rate = f"{len(got_reply)/applied_total*100:.0f}%" if applied_total else "—"

        # ---- Funnel stages ----
        def _cnt(*statuses):
            return sum(1 for j in all_applied if j.status in statuses)

        funnel = [
            {"label_zh": "Applied",    "label_en": "Applied",       "color": "#4f46e5", "count": applied_total},
            {"label_zh": "Phone Screen", "label_en": "Phone Screen",  "color": "#0891b2", "count": _cnt("phone_screen")},
            {"label_zh": "HR Interview",   "label_en": "HR Interview",  "color": "#7c3aed", "count": _cnt("hr_interview","interview")},
            {"label_zh": "HM Interview",   "label_en": "HM Interview",  "color": "#d97706", "count": _cnt("hm_interview")},
            {"label_zh": "Final Round",       "label_en": "Final Round",   "color": "#ea580c", "count": _cnt("final_round")},
            {"label_zh": "Offer",     "label_en": "Offer",          "color": "#16a34a", "count": _cnt("offer")},
        ]

        # ---- Recent activity (max 20, show only 10) ----
        def _job_to_timeline(j):
            meta = STAGE_META.get(j.status, {"label_zh": j.status, "label_en": j.status, "color": "#64748b", "bg": "#f1f5f9"})
            date = (j.applied_at or j.updated_at or j.created_at)
            return {
                "job_id": j.id,
                "title": j.title,
                "company": j.company,
                "date": date.strftime("%m/%d") if date else "—",
                "label_zh": meta["label_zh"],
                "label_en": meta["label_en"],
                "color": meta["color"],
                "bg": meta["bg"],
            }

        recent = [_job_to_timeline(j) for j in all_applied[:15]]

        # ---- Source distribution ----
        by_source = [{"source": row[0] or "unknown", "count": row[1]} for row in src_rows]

        # ---- In-progress list ----
        in_process_list = [_job_to_timeline(j) for j in in_process]

        # ---- Offer list ----
        offer_list = [_job_to_timeline(j) for j in offers]

        stats = {
            "applied_total": applied_total,
            "in_process": len(in_process),
            "offers": len(offers),
            "rejections": len(rejections),
            "response_rate": response_rate,
            "funnel": funnel,
            "by_source": by_source,
            "in_process_list": in_process_list,
            "offer_list": offer_list,
        }

        return render("stats.html", s=stats, recent=recent)

    @app.post("/job/{job_id}/status")
    def update_status(job_id: int, to: str = Form(...)):
        """Generic status switch. to in {shortlisted, applied, interview, offer, rejected, archived}"""
        allowed = {s.value for s in JobStatus}
        if to not in allowed:
            raise HTTPException(400, f"Invalid status {to}, allowed: {sorted(allowed)}")
        with session_scope(db_path) as session:
            job = session.get(Job, job_id)
            if not job:
                raise HTTPException(404, "Job does not exist")
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
        job_type: str = "all",   # all / full-time / internship / contract / part-time
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
            if job_type == "internship":
                # Match DB field OR title heuristic (for jobs collected before this column was added)
                from sqlalchemy import or_, func
                stmt = stmt.where(
                    or_(
                        Job.job_type == "internship",
                        func.lower(Job.title).contains("intern"),
                    )
                )
            elif job_type == "full-time":
                from sqlalchemy import and_, func
                stmt = stmt.where(
                    and_(
                        Job.job_type != "internship",
                        ~func.lower(Job.title).contains("intern"),
                    )
                )
            elif job_type not in ("all", ""):
                stmt = stmt.where(Job.job_type == job_type)
            jobs = list(session.scalars(stmt).all())
            for j in jobs:
                session.expunge(j)

            # Current filtered profile's label for banner display
            profile_label = None
            if profile_id is not None:
                from .db import Profile
                p = session.get(Profile, profile_id)
                profile_label = p.label if p else f"#{profile_id} (deleted)"

        sort_fn = SORTERS.get(sort, SORTERS["score"])
        jobs.sort(key=sort_fn)

        return render(
            "index.html",
            jobs=jobs,
            min_score=min_score,
            status=status,
            sort=sort,
            job_type=job_type,
            total=len(jobs),
            profile_id=profile_id,
            profile_label=profile_label,
        )

    # ---- Resume conversational refinement (must register before /job/{job_id}) ----

    @app.get("/job/{job_id}/refine")
    def refine_page(job_id: int):
        from .refine import get_revisions, get_current_resume_md, load_chat_history
        job = _get_job(job_id)
        revisions = get_revisions(config, job_id)
        current_md = get_current_resume_md(config, job_id) or ""
        # history is already in compact format (user=original request, assistant=change notes), use directly
        messages = load_chat_history(config, job_id)
        return render("refine.html",
                      job=job,
                      revisions=revisions,
                      current_md=current_md,
                      messages=messages)

    @app.post("/job/{job_id}/refine/chat")
    async def refine_chat(job_id: int, message: str = Form(...)):
        from .refine import chat_refine
        try:
            result = chat_refine(config, job_id, message)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/job/{job_id}/refine/clear")
    def refine_clear(job_id: int):
        from .refine import clear_chat_history
        clear_chat_history(config, job_id)
        return RedirectResponse(url=f"/job/{job_id}/refine", status_code=303)

    @app.get("/job/{job_id}/refine/version/{version_num}")
    def refine_version(job_id: int, version_num: int):
        from .refine import get_revision, get_current_resume_md
        # version_num=0 means original tailor output
        if version_num == 0:
            md = get_current_resume_md(config, job_id)
            if not md:
                raise HTTPException(404, "Original resume does not exist")
            return JSONResponse({"md_content": md, "note": "Tailor generated version",
                                 "version_num": 0, "created_at": ""})
        rev = get_revision(config, job_id, version_num)
        if not rev:
            raise HTTPException(404, "Version does not exist")
        return JSONResponse({"md_content": rev.md_content, "note": rev.note,
                             "version_num": rev.version_num,
                             "created_at": str(rev.created_at)})

    @app.get("/job/{job_id}/refine/version/{version_num}/pdf")
    def refine_version_pdf(job_id: int, version_num: int):
        from .refine import get_revision
        rev = get_revision(config, job_id, version_num)
        if not rev:
            raise HTTPException(404, "Version does not exist")
        import re as _re
        import tempfile, os
        safe_company = _re.sub(r"[^\w\-]+", "_", _get_job(job_id).company)[:30]
        pdf_path = config.path("outputs_dir") / f"{job_id:03d}_{safe_company}_v{version_num}.pdf"
        if not pdf_path.exists():
            try:
                md_to_pdf(rev.md_content, pdf_path)
            except Exception as e:
                raise HTTPException(500, f"PDF generation failed: {e}")
        return FileResponse(str(pdf_path), media_type="application/pdf",
                            filename=pdf_path.name)

    # ---- Manually add job (must register before /job/{job_id}, otherwise "add" parsed as int) ----

    @app.get("/job/add")
    def add_job_form(error: str = "", duplicate_id: int = 0, form_url: str = ""):
        duplicate = None
        if duplicate_id:
            with session_scope(config.path("db_path")) as session:
                dup = session.get(Job, duplicate_id)
                if dup:
                    session.expunge(dup)
                    duplicate = dup
        return render("add_job.html", error=error, duplicate=duplicate, form_url=form_url)

    @app.post("/job/add")
    async def add_job_submit(
        url: str = Form(default=""),
        jd_text: str = Form(default=""),
        user_note: str = Form(default=""),
        jd_file: UploadFile = File(default=None),
    ):
        from .manual_add import ManualJobInput, add_job_from_file, add_job_from_text

        raw_text = ""
        tmp_path = None
        if jd_file and jd_file.filename:
            content = await jd_file.read()
            if content:
                import tempfile
                suffix = Path(jd_file.filename).suffix.lower()
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                    f.write(content)
                    tmp_path = Path(f.name)
        elif jd_text.strip():
            raw_text = jd_text.strip()
        else:
            return RedirectResponse(url=f"/job/add?error=Please provide JD text or file&form_url={url}", status_code=303)

        try:
            if tmp_path:
                job, is_new = add_job_from_file(
                    config, tmp_path, url=url.strip(),
                    user_note=user_note.strip(), run_matcher=True,
                )
            else:
                inp = ManualJobInput(raw_text=raw_text, url=url.strip(), user_note=user_note.strip())
                job, is_new = add_job_from_text(config, inp, run_matcher=True)
        except Exception as e:
            return RedirectResponse(url=f"/job/add?error={str(e)[:120]}&form_url={url}", status_code=303)
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        if not is_new:
            return RedirectResponse(url=f"/job/add?duplicate_id={job.id}&form_url={url}", status_code=303)
        return RedirectResponse(url=f"/job/{job.id}", status_code=303)

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
            raise HTTPException(400, f"Cannot find resume: {e}")

        try:
            tailor_for_job(config, resume_text, job_id, candidate_name=name)
            if with_cover:
                write_cover_letter(config, resume_text, job_id)
        except Exception as e:
            raise HTTPException(500, f"tailor failed: {e}")

        return RedirectResponse(url=f"/job/{job_id}", status_code=303)

    @app.get("/job/{job_id}/pdf")
    def job_pdf(job_id: int):
        job = _get_job(job_id)
        if not job.tailored_resume_pdf_path:
            raise HTTPException(404, "PDF not generated yet, click Tailor first")
        path = Path(job.tailored_resume_pdf_path)
        if not path.exists():
            raise HTTPException(404, f"PDF file not found: {path}")
        return FileResponse(str(path), media_type="application/pdf", filename=path.name)

    @app.get("/job/{job_id}/md")
    def job_md(job_id: int):
        job = _get_job(job_id)
        if not job.tailored_resume_path:
            raise HTTPException(404, "MD not generated yet, click Tailor first")
        path = Path(job.tailored_resume_path)
        if not path.exists():
            raise HTTPException(404, f"MD file not found: {path}")
        return FileResponse(str(path), media_type="text/markdown", filename=path.name)

    @app.get("/job/{job_id}/cover")
    def job_cover(job_id: int):
        job = _get_job(job_id)
        if not job.cover_letter_path:
            raise HTTPException(404, "Cover letter not generated yet")
        path = Path(job.cover_letter_path)
        return FileResponse(str(path), media_type="text/plain", filename=path.name)

    @app.post("/job/{job_id}/mark-applied")
    def mark_app(job_id: int, note: Optional[str] = None):
        mark_applied(config, job_id, note=note)
        return RedirectResponse(url=f"/job/{job_id}", status_code=303)

    @app.post("/job/{job_id}/delete")
    def delete_job(job_id: int, redirect_to: str = Form(default="/jobs")):
        """Permanently delete a job and all its event records from the database.
        All counters automatically update on next page load (all calculated in real-time from DB).
        """
        with session_scope(config.path("db_path")) as session:
            job = session.get(Job, job_id)
            if job:
                session.delete(job)
                session.commit()
        return RedirectResponse(url=redirect_to, status_code=303)

    def _build_onboarding_context() -> dict[str, Any]:
        existing_desc = load_user_description(config) or ""
        profile = load_profile(config)

        # Old JSON auto-migration to DB (runs only once): if _profile.json exists but DB has no rows,
        # insert one history record (prevent recreating after deletion).
        existing_snapshots = list_profile_snapshots(config)
        if profile and not existing_snapshots:
            save_profile_snapshot(
                config, profile,
                user_description=existing_desc or "(auto-imported from old _profile.json)",
                resume_filename=None,
            )
            history = list_profile_snapshots(config)  # New row inserted, re-query
        else:
            history = existing_snapshots  # No insert, reuse existing, save one DB query
        current_id = get_current_profile_id(config)

        # Job counts per profile (displayed in history row)
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

        # All uploaded resumes, mtime descending (first is currently active)
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

        # Overall agent status
        has_profile = profile is not None
        has_schedule = cron_info["installed"] or scheduler.get_schedule_hours() > 0
        # Persistent state (includes cron-started processes)
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

        # Calculate elapsed time
        cur_elapsed = ""
        if cur_run and cur_run.get("started_at"):
            try:
                start = datetime.fromisoformat(cur_run["started_at"])
                secs = int((datetime.now() - start).total_seconds())
                cur_elapsed = f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60}s"
            except Exception:
                pass

        # ---- Platform metadata (for profile edit page) ----
        from .collect import PLATFORMS
        import json as _json
        _platform_meta = {
            "linkedin":     {"label": "LinkedIn",    "icon": "💼", "available": True},
            "indeed":       {"label": "Indeed",      "icon": "🔎", "available": True},
            "glassdoor":    {"label": "Glassdoor",   "icon": "🏢", "available": False},
            "ziprecruiter": {"label": "ZipRecruiter","icon": "📮", "available": False},
            "yc":           {"label": "YC Jobs",     "icon": "🚀", "available": True},
            "wellfound":    {"label": "Wellfound",   "icon": "🌊", "available": False},
            "dice":         {"label": "Dice",        "icon": "🎲", "available": True},
            "hackernews":   {"label": "HN",          "icon": "🔶", "available": True},
        }
        # 从 config 中读取实际 enabled 状态
        collectors_cfg = getattr(config, "collectors", None) or {}
        for pid, meta in _platform_meta.items():
            cfg = collectors_cfg.get(pid, {})
            if isinstance(cfg, dict):
                meta["available"] = bool(cfg.get("enabled", meta["available"]))
        all_platforms = [
            {"id": p, **_platform_meta.get(p, {"label": p, "icon": "🔗", "available": True})}
            for p in PLATFORMS
        ]
        # 默认启用平台
        default_platforms = [p["id"] for p in all_platforms if p["available"]]

        # 给 history profiles 挂上辅助属性
        for h in history:
            try:
                h.enabled_platforms_list = _json.loads(h.enabled_platforms) if h.enabled_platforms else default_platforms
            except Exception:
                h.enabled_platforms_list = default_platforms
            try:
                h.job_types_list = _json.loads(h.job_types_json) if h.job_types_json else ["Full-time"]
            except Exception:
                h.job_types_list = ["Full-time"]

        return {
            "existing_desc": existing_desc,
            "has_profile": profile is not None,
            "profiles": history,
            "supported_exts": ", ".join(sorted(SUPPORTED_EXTS)),
            "history": history,
            "current_id": current_id,
            "job_counts": job_counts,
            "pipeline_running": pipeline_state["running"],
            "resume_files": resume_files,
            "material_count": len(list(config.path("materials_dir").glob("*"))) if config.path("materials_dir").exists() else 0,
            "schedule": schedule_info,
            "freshness": freshness_info,
            "agent_state": agent_status,
            "persistent_state": persistent,
            "cur_run": cur_run,
            "last_run": last_run,
            "cur_elapsed": cur_elapsed,
            "health": _compute_health(),
            "counts": _compute_counts(),
            "lifetime": agent_state.get_lifetime_stats(config),
            "digest_to": (config.digest or {}).get("to"),
            "all_platforms": all_platforms,
            "default_platforms": default_platforms,
        }

    # ---- Onboarding Hub ----
    @app.get("/onboarding")
    def onboarding_hub():
        return render("onboarding.html", **_build_onboarding_context())

    @app.get("/onboarding/status")
    def onboarding_status():
        return render("onboarding_status.html", **_build_onboarding_context())

    @app.get("/onboarding/agents")
    def onboarding_agents():
        """Per-agent health dashboard."""
        h = _compute_health()
        persistent = agent_state.get_state(config)
        # Only show agent cards when pipeline is actively running
        has_any_run = bool(persistent.get("current_run"))
        agents = agent_state.get_agents_state(config) if has_any_run else {}
        timeouts = agent_state.AGENT_HEARTBEAT_TIMEOUT
        agents_with_timeout = {
            name: {**info, "heartbeat_timeout_sec": timeouts.get(name, agent_state.DEFAULT_HEARTBEAT_TIMEOUT)}
            for name, info in agents.items()
        }
        return render("onboarding_agents.html",
                      agents=agents_with_timeout,
                      has_any_run=has_any_run,
                      liveness=h["liveness"],
                      readiness=h["readiness"],
                      issues_live=h["issues_live"],
                      issues_ready=h["issues_ready"])

    @app.get("/onboarding/resumes")
    def onboarding_resumes():
        return render("onboarding_resumes.html", **_build_onboarding_context())

    # ---- Materials Library ----

    def _list_material_files() -> list[dict]:
        materials_dir = config.path("materials_dir")
        files = []
        for p in sorted(materials_dir.iterdir(), key=lambda x: -x.stat().st_mtime):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS \
                    and not p.name.startswith("_") and not p.name.startswith("."):
                files.append({
                    "filename": p.name,
                    "size_kb": round(p.stat().st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        return files

    @app.get("/onboarding/materials")
    def onboarding_materials(message: str = "", error: str = ""):
        return render("onboarding_materials.html",
                      material_files=_list_material_files(),
                      message=message, error=error)

    @app.post("/materials/upload")
    async def upload_material(material: UploadFile = File(...)):
        if not material.filename:
            return RedirectResponse("/onboarding/materials?error=No file selected", status_code=303)
        safe_name = _validate_resume_filename(material.filename)
        content = await material.read()
        if not content:
            return RedirectResponse("/onboarding/materials?error=File is empty", status_code=303)
        target = config.path("materials_dir") / safe_name
        target.write_bytes(content)
        return RedirectResponse(f"/onboarding/materials?message={safe_name} uploaded successfully", status_code=303)

    @app.post("/materials/{filename}/delete")
    def delete_material(filename: str):
        safe_name = _validate_resume_filename(filename)
        target = config.path("materials_dir") / safe_name
        if target.exists():
            target.unlink()
        return RedirectResponse(f"/onboarding/materials?message={safe_name} deleted", status_code=303)

    @app.get("/onboarding/profiles")
    def onboarding_profiles():
        return render("onboarding.html", **_build_onboarding_context())

    @app.get("/onboarding/automation")
    def onboarding_automation():
        return render("onboarding.html", **_build_onboarding_context())

    # ---- Profile Create / Edit ----
    @app.get("/onboarding/profile/new")
    def profile_new_form():
        ctx = _build_onboarding_context()
        return render("onboarding_profile.html", profile=None, **ctx)

    @app.get("/onboarding/profile/{profile_id}")
    def profile_edit_form(profile_id: int):
        import json as _json
        with session_scope(db_path) as session:
            from .db import Profile as ProfileModel
            row = session.get(ProfileModel, profile_id)
            if not row:
                raise HTTPException(404, f"Profile #{profile_id} does not exist")
            try:
                row.enabled_platforms_list = _json.loads(row.enabled_platforms) if row.enabled_platforms else None
            except Exception:
                row.enabled_platforms_list = None
            session.expunge(row)
        ctx = _build_onboarding_context()
        ctx["existing_desc"] = row.user_description  # Use this profile's description to override global default
        return render("onboarding_profile.html", profile=row, **ctx)

    @app.post("/onboarding/profile/new/submit")
    async def profile_new_submit(
        background_tasks: BackgroundTasks,
        description: str = Form(...),
        resume: UploadFile | None = File(None),
        resume_select: str = Form("__upload__"),
        platforms: list[str] = Form(default=[]),
        job_types: list[str] = Form(default=[]),
        schedule_hours: int = Form(24),
        action: str = Form("save_and_run"),
    ):
        """Create a new profile and optionally run immediately."""
        import json as _json
        from .collect import PLATFORMS as ALL_PLATFORMS

        desc = (description or "").strip()
        if not desc:
            raise HTTPException(400, "Please enter job search target description")

        # 1) Handle resume
        resume_dir = config.path("resume_dir")
        uploaded_filename: str | None = None
        if resume_select == "__upload__" or not resume_select:
            if resume and resume.filename:
                safe_filename = _validate_resume_filename(resume.filename)
                content = await resume.read()
                if not content:
                    raise HTTPException(400, "Uploaded file is empty")
                (resume_dir / safe_filename).write_bytes(content)
                uploaded_filename = safe_filename
                try:
                    parse_and_cache(resume_dir)
                except Exception as e:
                    raise HTTPException(500, f"Resume parsing failed: {e}")
        else:
            # Selected an existing resume — activate it
            target = resume_dir / resume_select
            if target.exists():
                target.touch()
                try:
                    parse_and_cache(resume_dir)
                except Exception:
                    pass
            uploaded_filename = resume_select

        # 2) Stop current pipeline
        if pipeline_state["running"]:
            _wait_for_cancel(timeout=60)

        # 3) Save description + analyze + create DB row
        save_user_description(config, desc)
        init_db(config.path("db_path"))

        enabled_plats = platforms if platforms else ALL_PLATFORMS
        enabled_json = _json.dumps(enabled_plats)

        # 4) Run analyze → collect → match in background, write Profile row (with schedule/platforms)
        def _run_with_extra():
            from .profile_analyzer import analyze_profile, save_profile_snapshot
            import json as _j
            # Reset pipeline_state immediately so processing page shows "analyzing" not last run's "done"
            with pipeline_lock:
                pipeline_state.update(
                    running=True,
                    phase="analyzing",
                    started_at=datetime.now(),
                    ended_at=None,
                    error=None,
                    stats={},
                    cancel_requested=False,
                    profile_id=None,
                    current_platform=None,
                )
            try:
                pa = analyze_profile(config, desc, job_types=job_types if job_types else ["Full-time"])
                save_profile(config, pa)
                pid = save_profile_snapshot(
                    config, pa,
                    user_description=desc,
                    resume_filename=uploaded_filename,
                )
                # Write schedule / platforms
                with session_scope(db_path) as session:
                    from .db import Profile as ProfileModel
                    row = session.get(ProfileModel, pid)
                    if row:
                        row.schedule_hours = schedule_hours
                        row.enabled_platforms = enabled_json
                        row.job_types_json = _json.dumps(job_types if job_types else ["Full-time"])
                        session.commit()
                # Update scheduler (use profile's own schedule_hours)
                scheduler.enable(hours=schedule_hours)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[profile_new] analyze failed: {e}")
                with pipeline_lock:
                    pipeline_state.update(running=False, phase="error", error=str(e), ended_at=datetime.now())
                return
            # Analysis done — release lock so _scheduled_run can take over for collect+match
            with pipeline_lock:
                pipeline_state.update(running=False, phase="analyzing")
            if action == "save_and_run":
                _scheduled_run(run_trends=False)

        background_tasks.add_task(_run_with_extra)
        if action == "save_and_run":
            return RedirectResponse(url="/onboarding/processing", status_code=303)
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/onboarding/profile/{profile_id}/save")
    async def profile_save(
        profile_id: int,
        background_tasks: BackgroundTasks,
        description: str = Form(...),
        resume: UploadFile | None = File(None),
        resume_select: str = Form("__upload__"),
        platforms: list[str] = Form(default=[]),
        job_types: list[str] = Form(default=[]),
        schedule_hours: int = Form(24),
        action: str = Form("save"),
    ):
        """Update existing profile description, resume, platforms, and scheduling settings."""
        import json as _json
        from .collect import PLATFORMS as ALL_PLATFORMS

        desc = (description or "").strip()
        if not desc:
            raise HTTPException(400, "Please enter job search target description")

        # Check profile exists
        with session_scope(db_path) as session:
            from .db import Profile as ProfileModel
            row = session.get(ProfileModel, profile_id)
            if not row:
                raise HTTPException(404, f"Profile #{profile_id} does not exist")

        # 1) Handle resume
        resume_dir = config.path("resume_dir")
        uploaded_filename: str | None = None
        if resume_select == "__upload__" or not resume_select:
            if resume and resume.filename:
                safe_filename = _validate_resume_filename(resume.filename)
                content = await resume.read()
                if not content:
                    raise HTTPException(400, "Uploaded file is empty")
                (resume_dir / safe_filename).write_bytes(content)
                uploaded_filename = safe_filename
                try:
                    parse_and_cache(resume_dir)
                except Exception:
                    pass
        else:
            uploaded_filename = resume_select
            target = resume_dir / resume_select
            if target.exists():
                target.touch()
                try:
                    parse_and_cache(resume_dir)
                except Exception:
                    pass

        # 2) Update DB row
        enabled_plats = platforms if platforms else ALL_PLATFORMS
        with session_scope(db_path) as session:
            from .db import Profile as ProfileModel
            row = session.get(ProfileModel, profile_id)
            row.user_description = desc
            row.label = desc[:60].strip()
            row.schedule_hours = schedule_hours
            row.enabled_platforms = _json.dumps(enabled_plats)
            row.job_types_json = _json.dumps(job_types if job_types else ["Full-time"])
            if uploaded_filename:
                row.resume_filename = uploaded_filename
            session.commit()

        # 3) If this is the current active profile, sync description file
        current_id = get_current_profile_id(config)
        if current_id == profile_id:
            save_user_description(config, desc)
            scheduler.enable(hours=schedule_hours)

        if action == "save_and_run":
            if pipeline_state["running"]:
                _wait_for_cancel(timeout=60)
            background_tasks.add_task(_scheduled_run, False)
            return RedirectResponse(url="/onboarding/processing", status_code=303)
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/onboarding/profile/{profile_id}/activate")
    def profile_activate(profile_id: int, background_tasks: BackgroundTasks):
        """Activate a profile, set scheduling based on its schedule_hours, and trigger first pipeline immediately."""
        try:
            activate_profile_snapshot(config, profile_id)
        except ValueError as e:
            raise HTTPException(404, str(e))

        # Read the profile's own schedule_hours
        profile_hours = 24  # Default
        with session_scope(db_path) as session:
            from .db import Profile as _P
            row = session.get(_P, profile_id)
            if row and row.schedule_hours is not None:
                profile_hours = int(row.schedule_hours)

        # Start scheduling based on profile's frequency
        scheduler.enable(hours=profile_hours)

        # Trigger pipeline immediately on activation regardless of manual or scheduled
        if not pipeline_state["running"]:
            threading.Thread(
                target=_scheduled_run, args=(False,),
                daemon=True, name="ActivatePipeline",
            ).start()
        return RedirectResponse(url="/onboarding", status_code=303)

    def _validate_resume_filename(filename: str) -> str:
        name = (filename or "").strip()
        if not name:
            raise HTTPException(400, "Filename is empty")
        if Path(name).name != name or "/" in name or "\\" in name:
            raise HTTPException(400, "Invalid filename")
        if name.startswith("_") or name.startswith("."):
            raise HTTPException(400, "Filename cannot start with _ or .")
        if Path(name).suffix.lower() not in SUPPORTED_EXTS:
            raise HTTPException(400, "Unsupported format")
        return name

    @app.post("/onboarding/submit")
    async def onboarding_submit(
        background_tasks: BackgroundTasks,
        description: str = Form(...),
        resume: UploadFile | None = File(None),
    ):
        # If current description is exactly the same as last time, don't run again
        existing = load_user_description(config) or ""
        desc = (description or "").strip()
        if not desc:
            raise HTTPException(400, "Please enter job search requirements description")

        if existing.strip() == desc and not (resume and resume.filename):
            # Description and resume haven't changed, go straight to home page
            return RedirectResponse(url="/", status_code=303)

        # If pipeline is running, stop it first
        if pipeline_state["running"]:
            print("[onboarding] Pipeline is running, requesting cancellation...")
            _wait_for_cancel(timeout=60)

        # 1) If new resume, save it
        resume_dir = config.path("resume_dir")
        uploaded_filename = None
        if resume and resume.filename:
            safe_filename = _validate_resume_filename(resume.filename)
            ext = Path(safe_filename).suffix.lower()
            if ext not in SUPPORTED_EXTS:
                raise HTTPException(
                    400,
                    f"Unsupported resume format {ext}. Supported: {sorted(SUPPORTED_EXTS)}",
                )
            content = await resume.read()
            if not content:
                raise HTTPException(400, "Uploaded file is empty")
            target = resume_dir / safe_filename
            target.write_bytes(content)
            uploaded_filename = safe_filename
            try:
                parse_and_cache(resume_dir)
            except Exception as e:
                raise HTTPException(500, f"Resume parsing failed: {e}")

        # 2) Save user description
        save_user_description(config, desc)

        # 3) Ensure DB exists
        init_db(config.path("db_path"))

        # 4) Don't block: run analyze + collect + match in background
        # Old onboarding doesn't have job_types selection, read default from config.yaml
        default_job_types = config.preferences.get("job_types") or ["Full-time"]
        background_tasks.add_task(_run_pipeline_bg, True, desc, uploaded_filename, default_job_types)

        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.get("/profiles/{profile_id}")
    def profile_detail(profile_id: int):
        """Single historical profile details: full description + Top-10 + regional companies + positions from this profile."""
        from .db import Profile
        from .profile_analyzer import ProfileAnalysis
        import json as _json
        with session_scope(db_path) as session:
            row = session.get(Profile, profile_id)
            if not row:
                raise HTTPException(404, f"Profile #{profile_id} does not exist")
            try:
                pa = ProfileAnalysis.from_dict(_json.loads(row.profile_json))
            except Exception:
                pa = None

            # All positions from this profile
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
        filename = _validate_resume_filename(filename)
        resume_dir = config.path("resume_dir")
        target = (resume_dir / filename).resolve()
        # Prevent path traversal
        if resume_dir.resolve() not in target.parents and target.parent != resume_dir.resolve():
            raise HTTPException(400, "Invalid filename")
        if not target.exists():
            raise HTTPException(404, f"Resume does not exist: {filename}")
        if target.suffix.lower() not in SUPPORTED_EXTS:
            raise HTTPException(400, "Unsupported format")
        return target

    @app.post("/resume/{filename}/activate")
    def activate_resume(filename: str, background_tasks: BackgroundTasks):
        """Activate a resume: touch + re-parse + run current profile through pipeline (collect + match)."""
        target = _safe_resume_path(filename)
        target.touch()
        try:
            parse_and_cache(config.path("resume_dir"))
        except Exception as e:
            raise HTTPException(500, f"Re-parsing failed: {e}")
        # Cancel running pipeline (if any), then start new pipeline
        if pipeline_state["running"]:
            _wait_for_cancel(timeout=60)
        background_tasks.add_task(_run_pipeline_bg, False, None, None)
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.post("/resume/{filename}/delete")
    def delete_resume(filename: str):
        filename = _validate_resume_filename(filename)
        # Try main directory first, if not found try _paused/
        resume_dir = config.path("resume_dir")
        active_target = resume_dir / filename
        if active_target.exists() and active_target.is_file():
            active_target.unlink()
        else:
            delete_paused_resume(resume_dir, filename)
        # If other active resumes exist, refresh cache; otherwise clear it
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
        filename = _validate_resume_filename(filename)
        resume_dir = config.path("resume_dir")
        try:
            pause_resume_file(resume_dir, filename)
        except FileNotFoundError:
            raise HTTPException(404, f"Resume does not exist: {filename}")
        # If current active resume was paused, next latest non-paused becomes active, refresh cache
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
        filename = _validate_resume_filename(filename)
        resume_dir = config.path("resume_dir")
        try:
            unpause_resume_file(resume_dir, filename)
        except FileNotFoundError:
            raise HTTPException(404, f"Not found in paused directory: {filename}")
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        # After restoring, refresh cache (it becomes the latest, i.e., new active)
        try:
            parse_and_cache(resume_dir)
        except Exception:
            pass
        return RedirectResponse(url="/onboarding", status_code=303)


    @app.post("/resume/upload")
    async def upload_resume_only(resume: UploadFile = File(...)):
        """Upload/replace resume separately, don't trigger pipeline. Any subsequent run-all / refresh uses the latest."""
        if not resume.filename:
            raise HTTPException(400, "No file selected")
        safe_filename = _validate_resume_filename(resume.filename)
        ext = Path(safe_filename).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            raise HTTPException(400, f"Unsupported format {ext}. Supported: {sorted(SUPPORTED_EXTS)}")
        content = await resume.read()
        if not content:
            raise HTTPException(400, "Uploaded file is empty")
        resume_dir = config.path("resume_dir")
        target = resume_dir / safe_filename
        target.write_bytes(content)
        # Force re-parse, refresh _parsed.txt cache
        try:
            parse_and_cache(resume_dir)
        except Exception as e:
            raise HTTPException(500, f"Parsing failed: {e}")
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/profiles/{profile_id}/use")
    def use_profile(profile_id: int, background_tasks: BackgroundTasks):
        """Switch back to a historical profile + re-run pipeline."""
        # Cancel current running pipeline (if any)
        if pipeline_state["running"]:
            print(f"[use_profile] Pipeline is running, requesting cancellation to switch to #{profile_id}...")
            _wait_for_cancel(timeout=60)
        try:
            activate_profile_snapshot(config, profile_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        # do_analyze=False: directly use activated profile to run collect+match
        background_tasks.add_task(_run_pipeline_bg, False, None, None)
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.post("/schedule/set")
    def set_schedule(hours: int = Form(...), backend: str = Form("cron")):
        """Set automatic scheduling.

        backend='cron'   : Write to system crontab (true 24/7 background)
        backend='inproc' : In-process scheduling (only runs when web server is running)
        """
        if backend == "cron":
            script = Path(__file__).resolve().parent.parent / "scripts" / "daily.sh"
            try:
                if hours == 0:
                    cron_uninstall()
                else:
                    cron_install(hours, script)
                # Also disable in-process scheduling to avoid double triggering
                scheduler.set_schedule_hours(0)
            except Exception as e:
                raise HTTPException(500, f"System cron operation failed: {e}")
        else:
            # In-process scheduling
            scheduler.set_schedule_hours(hours)
            # Disable cron to avoid double triggering
            try:
                cron_uninstall()
            except Exception:
                pass
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/freshness/set")
    def set_freshness(hours: int = Form(...)):
        """Set crawl time window (config.freshness will automatically read this value)."""
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

    # ===== Agent Global Control =====
    @app.post("/agent/pause")
    def agent_pause():
        """Pause agent: cancel running, disable in-process scheduling, uninstall cron. Don't touch data."""
        # 1. Cancel current pipeline
        if pipeline_state["running"]:
            pipeline_state["cancel_requested"] = True
        # 2. Disable in-process scheduling
        scheduler.set_schedule_hours(0)
        # 3. Uninstall system cron
        try:
            cron_uninstall()
        except Exception:
            pass
        return RedirectResponse(url="/onboarding", status_code=303)

    @app.post("/agent/delete")
    def agent_delete():
        """Delete agent: pause + clear profile + unbind all job profile_id, return to onboarding initial state.

        Don't delete Jobs data (historical positions still visible at /jobs), don't delete resume files.
        """
        # 1. Pause
        if pipeline_state["running"]:
            pipeline_state["cancel_requested"] = True
            _wait_for_cancel(timeout=30)
        scheduler.set_schedule_hours(0)
        try:
            cron_uninstall()
        except Exception:
            pass

        # 2. Clear profile
        resume_dir = config.path("resume_dir")
        for fname in ("_profile.json", "_user_description.txt"):
            f = resume_dir / fname
            if f.exists():
                f.unlink()
        # Deactivate all profiles in DB (but don't delete historical snapshots, users can reactivate)
        from sqlalchemy import update as _update
        from .db import Profile
        with session_scope(db_path) as session:
            session.execute(_update(Profile).where(Profile.is_current).values(is_current=False))
            session.commit()

        # 3. Clear pipeline state (memory + persistent)
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
        """Delete any profile. If deleting the current active one, automatically activate the next newest;
        if no other profiles exist, clear _profile.json + _user_description.txt, user needs to re-onboard.
        """
        from .db import Profile
        from sqlalchemy import update as _update
        was_current = False
        with session_scope(db_path) as session:
            row = session.get(Profile, profile_id)
            if not row:
                raise HTTPException(404, "Profile does not exist")
            was_current = bool(row.is_current)
            # Unbind all jobs under this profile
            session.execute(
                _update(Job).where(Job.profile_id == profile_id).values(profile_id=None)
            )
            session.delete(row)
            session.commit()

        # If current was deleted, activate next newest; if none, clear JSON
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
                    print(f"[delete] Auto-activate next profile failed: {e}")
            else:
                # No other profiles, clear JSON + user description, return to no-profile state
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
        """主页点 'Refresh' 时手动重跑完整 pipeline: collect + match + digest."""
        if pipeline_state["running"]:
            raise HTTPException(409, "已有流水线在跑")
        background_tasks.add_task(_scheduled_run, False)
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
    """Find an available port starting from start and incrementing."""
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
    raise RuntimeError(f"Cannot find available port in range {start}..{start + max_tries - 1}")


def run_server(config: Config, host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    actual_port = _find_free_port(host, port)
    if actual_port != port:
        print(f"Port {port} is in use, using {actual_port} instead")
    app = create_app(config)
    print(f"\n→ JobHunter web UI: http://{host}:{actual_port}\n")
    uvicorn.run(app, host=host, port=actual_port, log_level="info")
