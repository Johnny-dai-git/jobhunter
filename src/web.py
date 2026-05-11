"""本地 web UI: 查看岗位 + 点击触发 Claude 简历定制 + PDF.

启动: `python -m src.main web` -> http://127.0.0.1:8765

路由:
    GET  /                              首页, 列出 scored 岗位
    GET  /job/{id}                      单个岗位详情
    POST /job/{id}/tailor               触发 tailor (Claude 改简历 + PDF)
    GET  /job/{id}/pdf                  下载/查看 tailored resume PDF
    GET  /job/{id}/md                   下载/查看 tailored resume MD
    POST /job/{id}/mark-applied         标记已投递
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .config import Config
from .cover_letter import write_cover_letter
from .db import Job, JobStatus, session_scope
from .resume_reader import load_cached
from .tailor import tailor_for_job
from .tracker import mark_applied


TEMPLATES_DIR = Path(__file__).resolve().parent / "web_templates"


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Job Agent")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    db_path = config.path("db_path")

    def _get_job(job_id: int) -> Job:
        with session_scope(db_path) as session:
            job = session.get(Job, job_id)
            if not job:
                raise HTTPException(404, f"Job {job_id} 不存在")
            session.expunge(job)
            return job

    # ---- routes ----
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, min_score: float = 70.0, status: str = "active"):
        with session_scope(db_path) as session:
            stmt = select(Job)
            if status == "active":
                stmt = stmt.where(Job.status != JobStatus.ARCHIVED.value)
            elif status != "all":
                stmt = stmt.where(Job.status == status)
            if min_score is not None:
                stmt = stmt.where(Job.match_score >= min_score)
            stmt = stmt.order_by(Job.match_score.desc().nulls_last(), Job.id.desc())
            jobs = list(session.scalars(stmt).all())
            for j in jobs:
                session.expunge(j)

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "jobs": jobs,
                "min_score": min_score,
                "status": status,
                "total": len(jobs),
            },
        )

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int):
        job = _get_job(job_id)
        return templates.TemplateResponse(
            "job_detail.html",
            {
                "request": request,
                "job": job,
                "strengths_list": (job.match_strengths or "").splitlines(),
                "fit_bullets": (job.match_fit_bullets or "").splitlines(),
                "keywords_list": (job.match_keywords or "").splitlines(),
            },
        )

    @app.post("/job/{job_id}/tailor")
    def trigger_tailor(job_id: int, with_cover: bool = True, name: str = "Yuanjun Dai"):
        try:
            resume_text = load_cached(config.path("resume_dir"))
        except FileNotFoundError as e:
            raise HTTPException(400, f"找不到简历: {e}")

        try:
            tailor_for_job(config, resume_text, job_id, candidate_name=name)
            if with_cover:
                write_cover_letter(config, resume_text, job_id)
        except Exception as e:
            raise HTTPException(500, f"tailor 失败: {e}")

        return RedirectResponse(url=f"/job/{job_id}", status_code=303)

    @app.get("/job/{job_id}/pdf")
    def job_pdf(job_id: int):
        job = _get_job(job_id)
        if not job.tailored_resume_pdf_path:
            raise HTTPException(404, "PDF 还没生成,先点 Tailor")
        path = Path(job.tailored_resume_pdf_path)
        if not path.exists():
            raise HTTPException(404, f"PDF 文件不存在: {path}")
        return FileResponse(str(path), media_type="application/pdf", filename=path.name)

    @app.get("/job/{job_id}/md")
    def job_md(job_id: int):
        job = _get_job(job_id)
        if not job.tailored_resume_path:
            raise HTTPException(404, "MD 还没生成,先点 Tailor")
        path = Path(job.tailored_resume_path)
        if not path.exists():
            raise HTTPException(404, f"MD 文件不存在: {path}")
        return FileResponse(str(path), media_type="text/markdown", filename=path.name)

    @app.get("/job/{job_id}/cover")
    def job_cover(job_id: int):
        job = _get_job(job_id)
        if not job.cover_letter_path:
            raise HTTPException(404, "Cover letter 还没生成")
        path = Path(job.cover_letter_path)
        return FileResponse(str(path), media_type="text/plain", filename=path.name)

    @app.post("/job/{job_id}/mark-applied")
    def mark_app(job_id: int, note: Optional[str] = None):
        mark_applied(config, job_id, note=note)
        return RedirectResponse(url=f"/job/{job_id}", status_code=303)

    return app


def run_server(config: Config, host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    app = create_app(config)
    print(f"\n→ Job Agent web UI: http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
