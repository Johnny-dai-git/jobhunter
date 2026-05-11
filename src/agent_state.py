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
    }
}
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
    _save(config, state)


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
