"""跨进程的 agent 状态持久化.

问题: web server 用 in-memory pipeline_state 看自己跑的流水线 OK,
      但 cron 后台跑 (scripts/daily.sh) 时是另外的 Python 进程, web 不知道.

解法: 把状态写到 data/agent_state.json. 双方都读写这一份, web 打开就能看.

文件结构:
{
    "current_run": {       # 仅在跑的时候存在
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
    "last_run": {          # 最近一次跑完的快照
        "started_at": ISO,
        "ended_at": ISO,
        "duration_sec": int,
        "phase": "done|error|cancelled",
        "profile_id": ...,
        "trigger": ...,
        "stats": {...},
        "error": str | null
    },
    "agents": {            # per-agent 细粒度状态 (每次 run 开始时重置)
        "<agent_name>": {
            "status": "pending|running|done|error|skipped",
            "started_at": ISO | null,
            "ended_at": ISO | null,
            "heartbeat_at": ISO | null,   # 长操作中周期性更新, 用于 stuck 检测
            "duration_sec": int | null,
            "error": str | null,
            "meta": {}                    # agent 自定义字段 (processed_count 等)
        }
    }
}

Heartbeat 超时阈值 (秒):
  collection_agent: 300  (Apify 单个任务最长 600s, 每个平台完成后更新)
  matching_agent:   120  (每评一批岗位更新)
  其他 agent:        60
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
    """简单跨平台检测 pid 是否还活着 (用 signal 0)."""
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


# ── per-agent heartbeat 超时阈值 (秒) ─────────────────────────────────────
AGENT_HEARTBEAT_TIMEOUT: dict[str, int] = {
    "context_agent":    60,
    "collection_agent": 300,   # Apify 单任务最长 600s
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
    # 每次 run 开始时重置所有 agent 状态为 pending
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
    # 计算 duration
    if cur.get("started_at"):
        try:
            start = datetime.fromisoformat(cur["started_at"])
            cur["duration_sec"] = int((datetime.now() - start).total_seconds())
        except Exception:
            pass
    state["last_run"] = cur

    # 累计统计
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

    _save(config, state)


# ── Per-agent liveness / heartbeat ─────────────────────────────────────────

def agent_start(config, agent_name: str) -> None:
    """Agent 节点开始执行时调用."""
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
    """长操作中周期性调用, 更新心跳时间戳 + 可附加进度元数据."""
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
    """Agent 节点完成时调用."""
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
    """返回当前所有 agent 的状态快照, 带 stuck 检测."""
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
        # stuck 检测: running 状态但心跳超时
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
    """返回 agents 整体 liveness 摘要, 供 /health/liveness 使用."""
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
    """读状态, 顺便检测 orphan: 如果 current_run 的进程已经死了, 标记为 crashed."""
    state = _load(config)
    if "current_run" in state:
        pid = state["current_run"].get("pid")
        if pid and not _is_pid_alive(pid):
            # 进程不存在了, 移到 last_run + 标记 crashed
            cur = state.pop("current_run")
            cur.setdefault("ended_at", datetime.now().isoformat())
            cur["phase"] = "crashed"
            cur["error"] = f"进程 PID={pid} 已不存在 (可能 kill 或系统重启)"
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
    """删除整个状态文件 (用于 agent delete)."""
    p = state_path(config)
    if p.exists():
        p.unlink()
