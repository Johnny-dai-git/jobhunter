"""简历定制器: 针对单个岗位重写简历重点."""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select

from .agent import ClaudeClient, load_prompt, render
from .config import Config
from .db import Job, session_scope


def tailor_for_job(
    config: Config,
    resume_text: str,
    job_id: int,
    candidate_name: str = "Candidate",
) -> Path:
    """生成定制简历,写到 outputs 目录,返回文件路径."""
    client = ClaudeClient(config)
    db_path = config.path("db_path")
    outputs_dir = config.path("outputs_dir")

    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job id={job_id} 不存在")

        prompt = render(
            load_prompt("tailor"),
            resume=resume_text,
            title=job.title,
            company=job.company,
            description=job.description or "(无 JD)",
            candidate_name=candidate_name,
        )
        text = client.complete("tailor", prompt)

        # 文件名: 001_Acme_SoftwareEngineer.md
        safe_company = re.sub(r"[^\w\-]+", "_", job.company)[:40]
        safe_title = re.sub(r"[^\w\-]+", "_", job.title)[:40]
        out_path = outputs_dir / f"{job.id:03d}_{safe_company}_{safe_title}_resume.md"
        out_path.write_text(text, encoding="utf-8")

        job.tailored_resume_path = str(out_path)
        session.add(job)
        session.commit()

    return out_path
