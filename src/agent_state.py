"""Cross-process agent state persistence.

Problem: web server uses in-memory pipeline_state to monitor its own pipeline,
         but when cron backend runs (scripts/daily.sh), it's a separate Python process, so web doesn't know.

Solution: Write state to data/agent_state.json. Both read/write the same file, web can see it when it opens.

File structure:
{
    "current_run": {       # Only exists when running
        "started_at": ISO,
        "phase": "analyzing|collecting|matching|done|error|cancelled",
        "profile_id": int | null,
        "profile_label": str | null,
        "trigger": "web|cron|cli",
        "pid": int,
        "current_platform": str | null,
        "platform_started_at": ISO | null,
        "stats": {...}
    },
    "last_run": {          # Snapshot of most recent completed run
        "started_at": ISO,
        "ended_at": ISO,
        "duration_sec": int,
        "phase": "done|error|cancelled",
        "profile_id": ...,
        "trigger": ...,
        "stats": {...},
        "error": str | null
    },
    "agents": {            # Per-agent fine-grained state (reset at start of each run)
        "<agent_name>": {
            "status": "pending|running|done|error|skipped",
            "started_at": ISO | null,
            "ended_at": ISO | null,
            "heartbeat_at": ISO | null,   # Periodically updated during long operations, used for stuck detection
            "duration_sec": int | null,
            "error": str | null,
            "meta": {}                    # Agent-specific fields (processed_count, etc.)
        }
    }
}

Heartbeat timeout thresholds (seconds):
  collection_agent: 300  (Apify single task max 600s, updated after each platform completes)
  matching_agent:   120  (updated after each batch of jobs scored)
  Other agents:     60
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


STATE_FILENAME = "agent_state.json"


def state_path(config) -> Path:
    return config.path("resume_dir").parent / STATE_FILENAME


def _is_pid_alive(pid: int) -> bool:
    """Simple cross-platform check if pid is still alive (using signal 0)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _load(config) -> dict:
    p = state_path(config)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(config, state: dict) -> None:
    p = state_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


# ── Per-agent heartbeat timeout thresholds (seconds) ─────────────────────────────────────
AGENT_HEARTBEAT_TIMEOUT: dict[str, int] = {
    "context_agent":    60,
    "collection_agent": 900,   # Apify single task can take up to 600s, allow extra buffer
    "matching_agent":   120,
    "digest_agent":     60,
    "trend_agent":      90,
    "validation_agent": 30,
}
DEFAULT_HEARTBEAT_TIMEOUT = 120

ALL_AGENTS = list(AGENT_HEARTBEAT_TIMEOUT.keys())


def start_run(
    config,
    *,
    trigger: str = "web",
    profile_id: Optional[int] = None,
    profile_label: Optional[str] = None,
) -> None:
    state = _load(config)
    state["current_run"] = {
        "started_at": datetime.now().isoformat(),
        "phase": "starting",
        "profile_id": profile_id,
        "profile_label": profile_label,
        "trigger": trigger,
        "pid": os.getpid(),
        "current_platform": None,
        "platform_started_at": None,
        "stats": {},
    }
    # Reset all agent states to pending at start of each run
    state["agents"] = {
        name: {
            "status": "pending",
            "started_at": None,
            "ended_at": None,
            "heartbeat_at": None,
            "duration_sec": None,
            "error": None,
            "meta": {},
        }
        for name in ALL_AGENTS
    }
    _save(config, state)


def update_phase(config, phase: str, **extra) -> None:
    state = _load(config)
    cur = state.get("current_run") or {}
    cur["phase"] = phase
    for k, v in extra.items():
        if k == "stats" and isinstance(v, dict):
            cur.setdefault("stats", {}).update(v)
        else:
            cur[k] = v
    state["current_run"] = cur
    _save(config, state)


def set_platform(config, platform: Optional[str]) -> None:
    state = _load(config)
    cur = state.get("current_run") or {}
    cur["current_platform"] = platform
    cur["platform_started_at"] = datetime.now().isoformat() if platform else None
    state["current_run"] = cur
    _save(config, state)


def end_run(config, *, success: bool = True, phase: str = "done", error: Optional[str] = None) -> None:
    state = _load(config)
    cur = state.pop("current_run", None) or {}
    cur["ended_at"] = datetime.now().isoformat()
    cur["phase"] = phase if not success and phase == "done" else (
        "done" if success else phase
    )
    if not success and not error:
        cur["phase"] = phase or "error"
    if error:
        cur["error"] = str(error)
    # Calculate duration
    if cur.get("started_at"):
        try:
            start = datetime.fromisoformat(cur["started_at"])
            cur["duration_sec"] = int((datetime.now() - start).total_seconds())
        except Exception:
            pass
    state["last_run"] = cur

    # Cumulative statistics
    lifetime = state.setdefault("stats_lifetime", {
        "total_runs": 0, "successful_runs": 0, "failed_runs": 0,
        "first_run_at": None, "last_run_at": None,
        "total_duration_sec": 0,
    })
    lifetime["total_runs"] += 1
    if success:
        lifetime["successful_runs"] += 1
    else:
        lifetime["failed_runs"] += 1
    if not lifetime.get("first_run_at"):
        lifetime["first_run_at"] = cur.get("started_at")
    lifetime["last_run_at"] = cur.get("ended_at")
    lifetime["total_duration_sec"] += int(cur.get("duration_sec") or 0)

    # Clear per-agent states after run completes — health page shows blank until next run starts
    state["agents"] = {}

    _save(config, state)


# ── Per-agent liveness / heartbeat ─────────────────────────────────────────

def agent_start(config, agent_name: str) -> None:
    """Called when agent node starts execution."""
    state = _load(config)
    agents = state.setdefault("agents", {})
    agents[agent_name] = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "ended_at": None,
        "heartbeat_at": datetime.now().isoformat(),
        "duration_sec": None,
        "error": None,
        "meta": {},
    }
    _save(config, state)


def agent_heartbeat(config, agent_name: str, **meta) -> None:
    """Periodically called during long operations, updates heartbeat timestamp and optional progress metadata."""
    state = _load(config)
    agents = state.setdefault("agents", {})
    entry = agents.setdefault(agent_name, {})
    entry["heartbeat_at"] = datetime.now().isoformat()
    entry["status"] = "running"
    if meta:
        entry.setdefault("meta", {}).update(meta)
    _save(config, state)


def agent_end(
    config,
    agent_name: str,
    *,
    success: bool = True,
    skipped: bool = False,
    error: Optional[str] = None,
    **meta,
) -> None:
    """Called when agent node completes."""
    state = _load(config)
    agents = state.setdefault("agents", {})
    entry = agents.get(agent_name) or {}
    now = datetime.now()
    entry["ended_at"] = now.isoformat()
    entry["heartbeat_at"] = now.isoformat()
    entry["status"] = "skipped" if skipped else ("done" if success else "error")
    if error:
        entry["error"] = str(error)
    if entry.get("started_at"):
        try:
            start = datetime.fromisoformat(entry["started_at"])
            entry["duration_sec"] = int((now - start).total_seconds())
        except Exception:
            pass
    if meta:
        entry.setdefault("meta", {}).update(meta)
    agents[agent_name] = entry
    _save(config, state)


def get_agents_state(config) -> dict[str, dict]:
    """Return current state snapshot of all agents, including stuck detection."""
    state = _load(config)
    agents: dict[str, dict] = state.get("agents") or {}
    now = datetime.now()

    result = {}
    for name in ALL_AGENTS:
        entry = dict(agents.get(name) or {
            "status": "pending",
            "started_at": None,
            "ended_at": None,
            "heartbeat_at": None,
            "duration_sec": None,
            "error": None,
            "meta": {},
        })
        # Stuck detection: running state but heartbeat timed out
        if entry.get("status") == "running" and entry.get("heartbeat_at"):
            try:
                last_hb = datetime.fromisoformat(entry["heartbeat_at"])
                elapsed = (now - last_hb).total_seconds()
                timeout = AGENT_HEARTBEAT_TIMEOUT.get(name, DEFAULT_HEARTBEAT_TIMEOUT)
                entry["stuck"] = elapsed > timeout
                entry["heartbeat_age_sec"] = int(elapsed)
            except Exception:
                entry["stuck"] = False
        else:
            entry["stuck"] = False
        result[name] = entry
    return result


def get_agents_liveness(config) -> dict:
    """Return overall agents liveness summary for /health/liveness endpoint."""
    agents = get_agents_state(config)
    stuck = [name for name, a in agents.items() if a.get("stuck")]
    errored = [name for name, a in agents.items() if a.get("status") == "error"]
    return {
        "agents": agents,
        "any_stuck": bool(stuck),
        "any_error": bool(errored),
        "stuck_agents": stuck,
        "errored_agents": errored,
    }


def get_lifetime_stats(config) -> dict:
    state = _load(config)
    return state.get("stats_lifetime") or {
        "total_runs": 0, "successful_runs": 0, "failed_runs": 0,
        "first_run_at": None, "last_run_at": None,
        "total_duration_sec": 0,
    }


def get_state(config) -> dict:
    """Read state and detect orphans: if current_run process is dead, mark as crashed."""
    state = _load(config)
    if "current_run" in state:
        pid = state["current_run"].get("pid")
        if pid and not _is_pid_alive(pid):
            # Process no longer exists, move to last_run and mark crashed
            cur = state.pop("current_run")
            cur.setdefault("ended_at", datetime.now().isoformat())
            cur["phase"] = "crashed"
            cur["error"] = f"Process PID={pid} no longer exists (possibly killed or system restarted)"
            if cur.get("started_at"):
                try:
                    start = datetime.fromisoformat(cur["started_at"])
                    cur["duration_sec"] = int((datetime.now() - start).total_seconds())
                except Exception:
                    pass
            state["last_run"] = cur
            _save(config, state)
    return state


def clear(config) -> None:
    """Delete entire state file (used for agent delete)."""
    p = state_path(config)
    if p.exists():
        p.unlink()
