"""Application tracking tool.

job-agent doesn't auto-apply. After manually applying on websites, use `mark-applied` command
to tell agent "I applied to this", convenient for follow-up tracking and trend analysis.
"""
from __future__ import annotations

from datetime import datetime

from .config import Config
from .db import Event, Job, JobStatus, session_scope


def mark_applied(config: Config, job_id: int, note: str | None = None) -> None:
    """Mark a job's status as applied and record an event."""
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job id={job_id} does not exist")
        job.status = JobStatus.APPLIED.value
        job.applied_at = datetime.utcnow()
        session.add(Event(job_id=job_id, kind="applied", content=note))
        session.add(job)
        session.commit()
