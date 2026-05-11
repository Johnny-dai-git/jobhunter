"""求职信生成器.

借鉴 DailyJobMatch: 复用 matcher 阶段已经生成的 connector / fit_bullets,
避免 cover_letter 阶段再问一遍 LLM "你和这岗位什么连接".
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
            raise ValueError(f"Job id={job_id} 不存在")

        connector = job.match_connector or "(matcher 阶段未提取 connector, 请你自己从简历和 JD 中找一个具体连接点)"
        fit_bullets = job.match_fit_bullets or "(matcher 阶段未提取 fit_bullets, 请从简历中挑 3-5 条最相关的经历)"

        prompt = render(
            load_prompt("cover_letter"),
            resume=resume_text,
            title=job.title,
            company=job.company,
            location=job.location or "(未指定)",
            description=job.description or "(无 JD)",
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
