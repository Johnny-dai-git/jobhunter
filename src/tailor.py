"""简历定制器: 针对单个岗位重写简历重点 + 自动转 PDF.

输出 2 个文件:
- {id:03d}_{company}_{title}_resume.md   (源,可编辑)
- {id:03d}_{company}_{title}_resume.pdf  (用于投递)
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select

from .agent import ClaudeClient, load_prompt, render
from .config import Config
from .db import Job, session_scope
from .pdf_generator import md_to_pdf


def _extract_md_section(text: str) -> str:
    """从模型输出中抽出 ``` ... ``` 包裹的 markdown 主体. 没围栏就返回原文."""
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def tailor_for_job(
    config: Config,
    resume_text: str,
    job_id: int,
    candidate_name: str = "Candidate",
) -> Path:
    """生成定制简历: .md + .pdf 两份. 返回 .md 路径 (.pdf 路径存到 DB)."""
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
        md_body = _extract_md_section(text)

        # 文件名: 001_Acme_SoftwareEngineer
        safe_company = re.sub(r"[^\w\-]+", "_", job.company)[:40]
        safe_title = re.sub(r"[^\w\-]+", "_", job.title)[:40]
        base = f"{job.id:03d}_{safe_company}_{safe_title}_resume"

        md_path = outputs_dir / f"{base}.md"
        md_path.write_text(text, encoding="utf-8")  # 原始(含说明) 留 md
        pdf_path = outputs_dir / f"{base}.pdf"
        try:
            md_to_pdf(md_body, pdf_path)
        except Exception as e:
            print(f"[tailor] PDF 生成失败 ({e}), 只留 .md")
            pdf_path = None

        job.tailored_resume_path = str(md_path)
        if pdf_path:
            job.tailored_resume_pdf_path = str(pdf_path)
        session.add(job)
        session.commit()

    return md_path
