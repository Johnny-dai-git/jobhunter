"""投递状态追踪工具.

job-agent 不自动投递. 你在网站上手动投完之后,用 `mark-applied` 命令
告诉 agent "这个我投了",方便后续追踪和趋势分析.
"""
from __future__ import annotations

from datetime import datetime

from .config import Config
from .db import Event, Job, JobStatus, session_scope


def mark_applied(config: Config, job_id: int, note: str | None = None) -> None:
    """把某岗位状态改为 applied,并记一条事件."""
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job id={job_id} 不存在")
        job.status = JobStatus.APPLIED.value
        job.applied_at = datetime.utcnow()
        session.add(Event(job_id=job_id, kind="applied", content=note))
        session.add(job)
        session.commit()
