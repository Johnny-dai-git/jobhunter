"""Agent nodes used by the LangGraph orchestrator."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import agent_state
from ..collect import collect_all
from ..db import Profile, session_scope
from ..digest import run_digest
from ..matcher import score_pending
from ..resume_reader import load_cached
from ..trends import generate_report
from .state import JobAgentState
from .types import event


def _append_event(state: JobAgentState, agent: str, message: str, **data: Any) -> list:
    return [*state.get("events", []), event(agent, message, **data)]


def _start(config, name: str) -> None:
    try: agent_state.agent_start(config, name)
    except Exception: pass


def _hb(config, name: str, **meta) -> None:
    try: agent_state.agent_heartbeat(config, name, **meta)
    except Exception: pass


def _end(config, name: str, *, success: bool = True, skipped: bool = False,
         error: str | None = None, **meta) -> None:
    try: agent_state.agent_end(config, name, success=success, skipped=skipped, error=error, **meta)
    except Exception: pass


def context_agent(state: JobAgentState) -> JobAgentState:
    """Load profile metadata and resume text for downstream agents."""
    config = state["config"]
    options = state["options"]
    _start(config, "context_agent")
    try:
        profile_id = options.profile_id
        label = options.profile_label
        if profile_id and not label:
            with session_scope(config.path("db_path")) as session:
                row = session.get(Profile, profile_id)
                if row:
                    label = row.label
        _hb(config, "context_agent", step="loading_resume")
        try:
            resume_text = load_cached(config.path("resume_dir"))
        except FileNotFoundError:
            resume_text = ""
        _end(config, "context_agent", has_resume=bool(resume_text))
        return {
            **state,
            "profile_id": profile_id,
            "profile_label": label,
            "resume_text": resume_text,
            "events": _append_event(state, "context_agent", "Loaded run context",
                                    profile_id=profile_id, profile_label=label,
                                    has_resume=bool(resume_text)),
        }
    except Exception as exc:
        _end(config, "context_agent", success=False, error=str(exc))
        raise


def collection_agent(state: JobAgentState) -> JobAgentState:
    """Collect jobs from external platform agents/tools into the database."""
    config = state["config"]
    options = state["options"]

    if not options.collect:
        _start(config, "collection_agent")
        _end(config, "collection_agent", skipped=True)
        return {
            **state,
            "collect_stats": {"skipped": True},
            "events": _append_event(state, "collection_agent", "Skipped collection"),
        }

    _start(config, "collection_agent")
    try:
        agent_state.update_phase(config, "collecting")
    except Exception:
        pass

    platforms_done: list[str] = []

    def on_platform_start(name: str) -> None:
        try:
            agent_state.set_platform(config, name)
        except Exception:
            pass
        _hb(config, "collection_agent",
            current_platform=name,
            platforms_done=list(platforms_done))

    def on_platform_done(name: str, new: int) -> None:
        platforms_done.append(name)
        _hb(config, "collection_agent",
            current_platform=None,
            platforms_done=list(platforms_done),
            last_platform_new=new)

    try:
        stats = collect_all(
            config,
            options.platforms,
            on_platform_start=on_platform_start,
            on_platform_done=on_platform_done,
            profile_id=state.get("profile_id"),
        )
        try:
            agent_state.set_platform(config, None)
            agent_state.update_phase(config, "collecting", stats={"collect": stats})
        except Exception:
            pass
        _end(config, "collection_agent",
             total_new=stats.get("total_new", 0),
             total_seen=stats.get("total_seen", 0))
        return {
            **state,
            "collect_stats": stats,
            "events": _append_event(state, "collection_agent", "Collected platform jobs",
                                    total_new=stats.get("total_new", 0),
                                    total_seen=stats.get("total_seen", 0),
                                    total_excluded=stats.get("total_excluded", 0)),
        }
    except Exception as exc:
        _end(config, "collection_agent", success=False, error=str(exc))
        raise


def matching_agent(state: JobAgentState) -> JobAgentState:
    """Score pending jobs against the candidate profile."""
    config = state["config"]
    resume_text = state.get("resume_text") or ""
    if not resume_text:
        raise FileNotFoundError(
            "Resume cache not found. Run `python -m src.main parse-resume` first."
        )

    _start(config, "matching_agent")
    try:
        agent_state.update_phase(config, "matching")
    except Exception:
        pass

    scored_so_far = [0]

    def on_scored(job_id: int, score: float) -> None:
        scored_so_far[0] += 1
        if scored_so_far[0] % 5 == 0:  # 每 5 个更新一次心跳
            _hb(config, "matching_agent", scored=scored_so_far[0])

    try:
        results = score_pending(config, resume_text, on_scored=on_scored)
        stats = {"scored": len(results)}
        try:
            agent_state.update_phase(config, "matching", stats={"match": stats})
        except Exception:
            pass
        _end(config, "matching_agent", scored=len(results))
        return {
            **state,
            "match_stats": stats,
            "events": _append_event(state, "matching_agent", "Scored pending jobs",
                                    scored=len(results)),
        }
    except Exception as exc:
        _end(config, "matching_agent", success=False, error=str(exc))
        raise


def digest_agent(state: JobAgentState) -> JobAgentState:
    """Generate the daily top-job digest."""
    config = state["config"]
    options = state["options"]
    _start(config, "digest_agent")
    if not options.digest:
        _end(config, "digest_agent", skipped=True)
        return {**state, "events": _append_event(state, "digest_agent", "Skipped digest")}
    try:
        _hb(config, "digest_agent", step="generating")
        path = run_digest(config)
        _end(config, "digest_agent", path=str(path))
        return {
            **state,
            "digest_path": str(path),
            "events": _append_event(state, "digest_agent", "Generated digest", path=str(path)),
        }
    except Exception as exc:
        _end(config, "digest_agent", success=False, error=str(exc))
        raise


def trend_agent(state: JobAgentState) -> JobAgentState:
    """Generate market trend analysis from stored job evidence."""
    config = state["config"]
    options = state["options"]
    _start(config, "trend_agent")
    if not options.trends:
        _end(config, "trend_agent", skipped=True)
        return {**state, "events": _append_event(state, "trend_agent", "Skipped trend report")}
    try:
        _hb(config, "trend_agent", step="generating")
        paths = generate_report(
            config,
            days=options.trend_days,
            min_score=options.trend_min_score,
            formats=("md", "html"),
            send_email=True,
        )
        trend_paths = {fmt: str(path) for fmt, path in paths.items()}
        _end(config, "trend_agent", **trend_paths)
        return {
            **state,
            "trend_paths": trend_paths,
            "events": _append_event(state, "trend_agent", "Generated trend report", **trend_paths),
        }
    except Exception as exc:
        _end(config, "trend_agent", success=False, error=str(exc))
        raise


def validation_agent(state: JobAgentState) -> JobAgentState:
    """Validate that generated artifacts exist and expose run limitations."""
    config = state["config"]
    _start(config, "validation_agent")
    missing: list[str] = []

    digest_path = state.get("digest_path")
    if digest_path and not Path(digest_path).exists():
        missing.append(digest_path)
    for path in (state.get("trend_paths") or {}).values():
        if not Path(path).exists():
            missing.append(path)

    validation = {
        "ok": not missing,
        "missing_artifacts": missing,
        "limitations": [
            "Trend claims are scoped to collected platform data and configured filters.",
            "LLM output explains measured evidence; counts are computed by Python.",
        ],
    }
    _end(config, "validation_agent", ok=validation["ok"], missing=len(missing))
    return {
        **state,
        "validation": validation,
        "events": _append_event(state, "validation_agent", "Validated graph outputs",
                                ok=validation["ok"], missing_artifacts=missing),
    }
