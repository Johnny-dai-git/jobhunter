"""Small typed objects used by multi-agent orchestration."""
from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict


class AgentEvent(TypedDict, total=False):
    agent: str
    message: str
    data: dict[str, Any] | None
    at: str


def event(agent: str, message: str, **data: Any) -> AgentEvent:
    return {
        "agent": agent,
        "message": message,
        "data": data or None,
        "at": datetime.utcnow().isoformat(timespec="seconds"),
    }
