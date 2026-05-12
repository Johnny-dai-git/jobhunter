"""Cover letter generator.

Following DailyJobMatch: reuse connector / fit_bullets already generated during matcher phase,
avoid asking LLM again during cover_letter phase "what's your connection to this job".
"""
from __future__ import annotations

import re
from pathlib import Path

from .agent import ClaudeClient, load_prompt, render
from .config import Config
from .db import Job, session_scope


def write_cover_letter(config: Config, resume_text: str, job_id: int) -> Path:
    client = ClaudeClient(config)
    db_path = config.path("db_path")
    outputs_dir = config.path("outputs_dir")

    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job id={job_id} does not exist")

        connector = job.match_connector or "(Connector not extracted during matcher phase, please find a concrete connection point from resume and JD)"
        fit_bullets = job.match_fit_bullets or "(Fit bullets not extracted during matcher phase, please pick 3-5 most relevant experiences from resume)"

        prompt = render(
            load_prompt("cover_letter"),
            resume=resume_text,
            title=job.title,
            company=job.company,
            location=job.location or "(unspecified)",
            description=job.description or "(no JD)",
            connector=connector,
            fit_bullets=fit_bullets,
        )
        text = client.complete("cover_letter", prompt)

        safe_company = re.sub(r"[^\w\-]+", "_", job.company)[:40]
        safe_title = re.sub(r"[^\w\-]+", "_", job.title)[:40]
        out_path = outputs_dir / f"{job.id:03d}_{safe_company}_{safe_title}_cover.txt"
        out_path.write_text(text, encoding="utf-8")

        job.cover_letter_path = str(out_path)
        session.add(job)
        session.commit()

    return out_path
