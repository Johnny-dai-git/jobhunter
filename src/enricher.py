"""补填工具: 对已经评分但缺 work_mode/min_education 的旧岗位,只跑抽取不重新评分.

新岗位会在 matcher 阶段自动填这些字段. 这个工具只针对历史数据.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from .agent import make_client
from .config import Config
from .db import Job, JobStatus, session_scope


ENRICH_TOOL: dict[str, Any] = {
    "name": "submit_enrichment",
    "description": "从 JD 抽取 work_mode 和 min_education",
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


PROMPT = """从下面这段岗位描述 (JD) 里抽取 2 个字段:

- work_mode: remote / hybrid / onsite / unspecified
  ("Remote", "Fully remote", "Work from anywhere" → remote)
  ("Hybrid (3 days in office)" 等混合表述 → hybrid)
  ("On-site", "in-office", 列出明确办公地点且无远程描述 → onsite)
  (JD 未明说就 unspecified)

- min_education: high_school / bachelor / master / phd / any / unspecified
  ("PhD required" → phd, "MS preferred" → master, "Bachelor's required" → bachelor)
  ("Or equivalent experience" 但有学位要求 → 取明示学位)
  (JD 没提学位 → unspecified)

调用 submit_enrichment 工具.

JD:
---
{description}
---
"""


def enrich_pending(config: Config, *, limit: int | None = None) -> int:
    """对已评分但 work_mode 缺的 Job 调一次 LLM 抽取这两个字段."""
    client, model_name = make_client(config, "matcher")
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
        print(f"[enrich] {len(jobs)} 个岗位待补填")

        for job in jobs:
            try:
                resp = client.messages.create(
                    model=model_name,
                    max_tokens=200,
                    tools=[ENRICH_TOOL],
                    tool_choice={"type": "tool", "name": "submit_enrichment"},
                    messages=[
                        {
                            "role": "user",
                            "content": PROMPT.format(description=(job.description or "")[:3000]),
                        }
                    ],
                )
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
                print(f"  [!] #{job.id} 抽取失败: {e}")

    return done
