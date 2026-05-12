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


def context_agent(state: JobAgentState) -> JobAgentState:
    """Load profile metadata and resume text for downstream agents."""
    config = state["config"]
    options = state["options"]
    profile_id = options.profile_id
    label = options.profile_label

    if profile_id and not label:
        with session_scope(config.path("db_path")) as session:
            row = session.get(Profile, profile_id)
            if row:
                label = row.label

    try:
        resume_text = load_cached(config.path("resume_dir"))
    except FileNotFoundError:
        resume_text = ""

    return {
        **state,
        "profile_id": profile_id,
        "profile_label": label,
        "resume_text": resume_text,
        "events": _append_event(
            state,
            "context_agent",
            "Loaded run context",
            profile_id=profile_id,
            profile_label=label,
            has_resume=bool(resume_text),
        ),
    }


def collection_agent(state: JobAgentState) -> JobAgentState:
    """Collect jobs from external platform agents/tools into the database."""
    config = state["config"]
    options = state["options"]

    if not options.collect:
        return {
            **state,
            "collect_stats": {"skipped": True},
            "events": _append_event(state, "collection_agent", "Skipped collection"),
        }

    try:
        agent_state.update_phase(config, "collecting")
    except Exception:
        pass

    def on_platform_start(name: str) -> None:
        try:
            agent_state.set_platform(config, name)
        except Exception:
            pass

    stats = collect_all(
        config,
        options.platforms,
        on_platform_start=on_platform_start,
        profile_id=state.get("profile_id"),
    )

    try:
        agent_state.set_platform(config, None)
        agent_state.update_phase(config, "collecting", stats={"collect": stats})
    except Exception:
        pass

    return {
        **state,
        "collect_stats": stats,
        "events": _append_event(
            state,
            "collection_agent",
            "Collected platform jobs",
            total_new=stats.get("total_new", 0),
            total_seen=stats.get("total_seen", 0),
            total_excluded=stats.get("total_excluded", 0),
        ),
    }


def matching_agent(state: JobAgentState) -> JobAgentState:
    """Score pending jobs against the candidate profile."""
    config = state["config"]
    resume_text = state.get("resume_text") or ""
    if not resume_text:
        raise FileNotFoundError(
            "Resume cache not found. Run `python -m src.main parse-resume` first."
        )

    try:
        agent_state.update_phase(config, "matching")
    except Exception:
        pass

    results = score_pending(config, resume_text)
    stats = {"scored": len(results)}

    try:
        agent_state.update_phase(config, "matching", stats={"match": stats})
    except Exception:
        pass

    return {
        **state,
        "match_stats": stats,
        "events": _append_event(
            state,
            "matching_agent",
            "Scored pending jobs",
            scored=len(results),
        ),
    }


def digest_agent(state: JobAgentState) -> JobAgentState:
    """Generate the daily top-job digest."""
    options = state["options"]
    if not options.digest:
        return {
            **state,
            "events": _append_event(state, "digest_agent", "Skipped digest"),
        }

    path = run_digest(state["config"])
    return {
        **state,
        "digest_path": str(path),
        "events": _append_event(state, "digest_agent", "Generated digest", path=str(path)),
    }


def trend_agent(state: JobAgentState) -> JobAgentState:
    """Generate market trend analysis from stored job evidence."""
    options = state["options"]
    if not options.trends:
        return {
            **state,
            "events": _append_event(state, "trend_agent", "Skipped trend report"),
        }

    paths = generate_report(
        state["config"],
        days=options.trend_days,
        min_score=options.trend_min_score,
        formats=("md", "html"),
        send_email=True,
    )
    trend_paths = {fmt: str(path) for fmt, path in paths.items()}
    return {
        **state,
        "trend_paths": trend_paths,
        "events": _append_event(
            state,
            "trend_agent",
            "Generated trend report",
            **trend_paths,
        ),
    }


def validation_agent(state: JobAgentState) -> JobAgentState:
    """Validate that generated artifacts exist and expose run limitations."""
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

    return {
        **state,
        "validation": validation,
        "events": _append_event(
            state,
            "validation_agent",
            "Validated graph outputs",
            ok=validation["ok"],
            missing_artifacts=missing,
        ),
    }
