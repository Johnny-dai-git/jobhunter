"""LangGraph orchestration for the job-search multi-agent workflow."""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .. import agent_state
from .nodes import (
    collection_agent,
    context_agent,
    digest_agent,
    matching_agent,
    trend_agent,
    validation_agent,
)
from .state import JobAgentRunOptions, JobAgentState
from .types import event


def _after_matching(state: JobAgentState) -> str:
    options = state["options"]
    if options.digest:
        return "digest_agent"
    if options.trends:
        return "trend_agent"
    return "validation_agent"


def _after_digest(state: JobAgentState) -> str:
    return "trend_agent" if state["options"].trends else "validation_agent"


def build_job_agent_graph():
    """Build the compiled multi-agent graph.

    Graph shape:
    context -> collection -> matching -> digest? -> trend? -> validation
    """
    graph = StateGraph(JobAgentState)
    graph.add_node("context_agent", context_agent)
    graph.add_node("collection_agent", collection_agent)
    graph.add_node("matching_agent", matching_agent)
    graph.add_node("digest_agent", digest_agent)
    graph.add_node("trend_agent", trend_agent)
    graph.add_node("validation_agent", validation_agent)

    graph.add_edge(START, "context_agent")
    graph.add_edge("context_agent", "collection_agent")
    graph.add_edge("collection_agent", "matching_agent")
    graph.add_conditional_edges(
        "matching_agent",
        _after_matching,
        {
            "digest_agent": "digest_agent",
            "trend_agent": "trend_agent",
            "validation_agent": "validation_agent",
        },
    )
    graph.add_conditional_edges(
        "digest_agent",
        _after_digest,
        {
            "trend_agent": "trend_agent",
            "validation_agent": "validation_agent",
        },
    )
    graph.add_edge("trend_agent", "validation_agent")
    graph.add_edge("validation_agent", END)
    return graph.compile()


def run_job_agent_graph(config: Any, options: JobAgentRunOptions) -> JobAgentState:
    """Run the LangGraph workflow and persist high-level run status."""
    try:
        agent_state.start_run(
            config,
            trigger=options.trigger,
            profile_id=options.profile_id,
            profile_label=options.profile_label,
        )
    except Exception:
        pass

    initial_state: JobAgentState = {
        "config": config,
        "options": options,
        "errors": [],
        "events": [event("orchestrator", "Started multi-agent graph")],
    }

    try:
        final_state = build_job_agent_graph().invoke(initial_state)
        try:
            agent_state.end_run(config, success=True, phase="done")
        except Exception:
            pass
        return final_state
    except Exception as exc:
        try:
            agent_state.end_run(config, success=False, phase="error", error=str(exc))
        except Exception:
            pass
        raise
