"""Enrichment tool: for old scored jobs missing work_mode/min_education, run extraction only without re-scoring.

New jobs auto-fill these during matcher phase. This tool is for historical data only.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from .agent import make_client
from .config import Config
from .db import Job, JobStatus, session_scope


ENRICH_TOOL: dict[str, Any] = {
    "name": "submit_enrichment",
    "description": "Extract work_mode and min_education from JD",
    "input_schema": {
        "type": "object",
        "properties": {
            "work_mode": {
                "type": "string",
                "enum": ["remote", "hybrid", "onsite", "unspecified"],
            },
            "min_education": {
                "type": "string",
                "enum": ["high_school", "bachelor", "master", "phd", "any", "unspecified"],
            },
        },
        "required": ["work_mode", "min_education"],
    },
}


PROMPT = """Extract 2 fields from the job description (JD) below:

- work_mode: remote / hybrid / onsite / unspecified
  ("Remote", "Fully remote", "Work from anywhere" → remote)
  ("Hybrid (3 days in office)" and similar hybrid expressions → hybrid)
  ("On-site", "in-office", explicitly states office location without remote → onsite)
  (If JD doesn't specify → unspecified)

- min_education: high_school / bachelor / master / phd / any / unspecified
  ("PhD required" → phd, "MS preferred" → master, "Bachelor's required" → bachelor)
  ("Or equivalent experience" but has degree requirement → use stated degree)
  (If JD doesn't mention education → unspecified)

Call the submit_enrichment tool.

JD:
---
{description}
---
"""


def enrich_pending(config: Config, *, limit: int | None = None) -> int:
    """For scored jobs missing work_mode, call LLM once to extract both fields."""
    import json
    client, model_name, provider = make_client(config, "matcher")
    db_path = config.path("db_path")
    done = 0

    with session_scope(db_path) as session:
        stmt = select(Job).where(
            Job.status != JobStatus.ARCHIVED.value,
            Job.description.is_not(None),
            or_(Job.work_mode.is_(None), Job.work_mode == ""),
        )
        if limit:
            stmt = stmt.limit(limit)
        jobs = session.scalars(stmt).all()
        print(f"[enrich] {len(jobs)} jobs pending enrichment")

        for job in jobs:
            try:
                prompt = PROMPT.format(description=(job.description or "")[:3000])
                if provider == "deepseek":
                    # OpenAI format
                    from .agent import _convert_tool_to_openai
                    tools = [_convert_tool_to_openai(ENRICH_TOOL)]
                    with client.chat.completions.stream(
                        model=model_name,
                        max_tokens=200,
                        tools=tools,
                        tool_choice={"type": "function", "function": {"name": "submit_enrichment"}},
                        messages=[{"role": "user", "content": prompt}],
                    ) as stream:
                        resp = stream.get_final_completion()
                    if resp.choices[0].message.tool_calls:
                        tool_call = resp.choices[0].message.tool_calls[0]
                        data = json.loads(tool_call.function.arguments)
                        job.work_mode = data.get("work_mode", "unspecified")
                        job.min_education = data.get("min_education", "unspecified")
                        session.add(job)
                        session.commit()
                        done += 1
                        print(f"  + #{job.id} {job.title}: {job.work_mode} / {job.min_education}")
                else:
                    # Anthropic format
                    with client.messages.stream(
                        model=model_name,
                        max_tokens=200,
                        tools=[ENRICH_TOOL],
                        tool_choice={"type": "tool", "name": "submit_enrichment"},
                        messages=[{"role": "user", "content": prompt}],
                    ) as _s:
                        resp = _s.get_final_message()
                    for block in resp.content:
                        if getattr(block, "type", None) == "tool_use" and block.name == "submit_enrichment":
                            data = block.input or {}
                            job.work_mode = data.get("work_mode", "unspecified")
                            job.min_education = data.get("min_education", "unspecified")
                            session.add(job)
                            session.commit()
                            done += 1
                            print(f"  + #{job.id} {job.title}: {job.work_mode} / {job.min_education}")
                            break
            except Exception as e:
                print(f"  [!] #{job.id} extraction failed: {e}")

    return done
