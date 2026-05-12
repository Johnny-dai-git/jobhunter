"""Shared state passed between LangGraph agent nodes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, TypedDict

from .types import AgentEvent


@dataclass(frozen=True)
class JobAgentRunOptions:
    """Runtime options for one graph execution."""

    platforms: list[str]
    collect: bool = True
    digest: bool = True
    trends: bool = True
    trend_days: int = 30
    trend_min_score: float = 50.0
    trigger: str = "cli"
    profile_id: Optional[int] = None
    profile_label: Optional[str] = None


class JobAgentState(TypedDict, total=False):
    """Graph state.

    The current graph runs in-process and carries the loaded Config object.
    If we later add a persistent LangGraph checkpointer, store config/options
    identifiers instead of these runtime objects.
    """

    config: Any
    options: JobAgentRunOptions
    profile_id: Optional[int]
    profile_label: Optional[str]
    resume_text: str
    collect_stats: dict[str, Any]
    match_stats: dict[str, Any]
    digest_path: str
    trend_paths: dict[str, str]
    validation: dict[str, Any]
    errors: list[str]
    events: list[AgentEvent]
