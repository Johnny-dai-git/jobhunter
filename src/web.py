"""本地 web UI: 查看岗位 + 点击触发 Claude 简历定制 + PDF.

启动: `python -m src.main web` -> http://127.0.0.1:8765

为了避开 fastapi/starlette/jinja2 版本冲突坑, 这版**不用 Jinja2Templates**,
直接用 jinja2 自己渲染字符串后塞回 HTMLResponse.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from .collect import collect_all
from .config import Config
from .cover_letter import write_cover_letter
from .db import Job, JobStatus, init_db, session_scope
from .matcher import score_pending
from .profile_analyzer import (
    activate_profile_snapshot,
    analyze_profile,
    get_current_profile_id,
    list_profile_snapshots,
    load_profile,
    load_user_description,
    save_profile,
    save_profile_snapshot,
    save_user_description,
)
from .resume_reader import SUPPORTED_EXTS, load_cached, parse_and_cache
from .tailor import tailor_for_job
from .tracker import mark_applied


TEMPLATES_DIR = Path(__file__).resolve().parent / "web_templates"


# ========= 排序辅助 =========
_EDU_RANK = {
    "phd": 5,
    "master": 4,
    "bachelor": 3,
    "high_school": 2,
    "any": 1,
    "unspecified": 0,
    "": 0,
    None: 0,
}


def _edu_rank(val: str | None) -> int:
    return _EDU_RANK.get((val or "").lower(), 0)


def _parse_salary_min(s: str | None) -> int:
    """从'$120,000 - $150,000' / '80K - 110K USD' 等字符串里抽 minimum 数字 (USD).

    用于排序; 抓不到就返回 0.
    """
    import re
    if not s:
        return 0
    nums: list[int] = []
    for m in re.finditer(r"\$?\s*(\d{1,3}(?:[,\.]\d{3})+|\d+)\s*([Kk])?", s):
        raw = m.group(1).replace(",", "").replace(".", "")
        try:
            n = int(raw)
        except ValueError:
            continue
        if m.group(2):  # K/k 后缀
            n *= 1000
        elif n < 1000:  # 纯数字 < 1000 (例如 "120") 视作 K
            n *= 1000
        if 10_000 <= n <= 1_500_000:
            nums.append(n)
    return min(nums) if nums else 0


_MODE_RANK = {"remote": 3, "hybrid": 2, "onsite": 1, "unspecified": 0, "": 0, None: 0}


def _mode_rank(val: str | None) -> int:
    return _MODE_RANK.get((val or "").lower(), 0)


SORTERS = {
    "score": lambda j: -(j.match_score or -1),
    "salary": lambda j: -_parse_salary_min(j.salary),
    "education": lambda j: -_edu_rank(j.min_education),
    "mode": lambda j: -_mode_rank(j.work_mode),
    "posted": lambda j: -(j.posted_at.timestamp() if j.posted_at else 0),
    "company": lambda j: (j.company or "").lower(),
}


def _make_env() -> Environment:
    """直接构造 Jinja2 Environment, 不走 starlette wrapper, 避开 cache_key bug."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        cache_size=0,  # 禁用缓存彻底避坑
    )


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="JobHunter")
    env = _make_env()
    db_path = config.path("db_path")

    # ===== 后台流水线状态 (单实例够用,多用户场景需换 DB) =====
    pipeline_state: dict[str, Any] = {
        "running": False,
        "phase": "idle",            # idle | analyzing | collecting | matching | done | cancelled | error
        "started_at": None,
        "ended_at": None,
        "error": None,
        "stats": {},
        "profile_id": None,         # 当前流水线绑定的画像 id
        "cancel_requested": False,
    }
    pipeline_lock = threading.Lock()

    def _should_continue():
        return not pipeline_state["cancel_requested"]

    def _run_pipeline_bg(
        do_analyze: bool,
        user_description: str | None = None,
        resume_filename: str | None = None,
    ):
        """后台跑 [analyze_profile] -> collect -> match. 失败也能 graceful exit."""
        with pipeline_lock:
            if pipeline_state["running"]:
                return  # 防止重入
            pipeline_state.update(
                running=True,
                phase="analyzing" if do_analyze else "collecting",
                started_at=datetime.now(),
                ended_at=None,
                error=None,
                stats={},
                cancel_requested=False,
                profile_id=None,
            )

        try:
            if do_analyze:
                profile = analyze_profile(config, user_description=user_description)
                save_profile(config, profile)
                # 同时写历史 DB
                pid = save_profile_snapshot(
                    config, profile,
                    user_description=user_description or "",
                    resume_filename=resume_filename,
                )
                pipeline_state["profile_id"] = pid
            else:
                # 沿用现有当前画像 id
                pipeline_state["profile_id"] = get_current_profile_id(config)

            if not _should_continue():
                pipeline_state["phase"] = "cancelled"
                return

            pipeline_state["phase"] = "collecting"
            stats = collect_all(
                config,
                should_continue=_should_continue,
                profile_id=pipeline_state["profile_id"],
            )
            pipeline_state["stats"]["collect"] = stats

            if not _should_continue():
                pipeline_state["phase"] = "cancelled"
                return

            pipeline_state["phase"] = "matching"
            resume_text = load_cached(config.path("resume_dir"))
            results = score_pending(config, resume_text, should_continue=_should_continue)
            pipeline_state["stats"]["match"] = {"scored": len(results)}

            pipeline_state["phase"] = "cancelled" if not _should_continue() else "done"
        except Exception as e:
            import traceback
            traceback.print_exc()
            pipeline_state["phase"] = "error"
            pipeline_state["error"] = str(e)
        finally:
            pipeline_state["running"] = False
            pipeline_state["ended_at"] = datetime.now()


    def _wait_for_cancel(timeout: float = 30) -> bool:
        """请求取消,轮询等到流水线真停下来(或超时). 返回是否成功停下."""
        import time as _time
        if not pipeline_state["running"]:
            return True
        pipeline_state["cancel_requested"] = True
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if not pipeline_state["running"]:
                return True
            _time.sleep(0.5)
        return False

    def render(name: str, **ctx) -> HTMLResponse:
        tpl = env.get_template(name)
        return HTMLResponse(tpl.render(**ctx))

    def _get_job(job_id: int) -> Job:
        with session_scope(db_path) as session:
            job = session.get(Job, job_id)
            if not job:
                raise HTTPException(404, f"Job {job_id} 不存在")
            session.expunge(job)
            return job

    # ---- routes ----
    @app.get("/")
    def index(
        min_score: float = 70.0,
        status: str = "active",
        sort: str = "score",
    ):
        # 首次访问无 profile 则跳 onboarding
        if not load_profile(config):
            return RedirectResponse(url="/onboarding", status_code=303)

        with session_scope(db_path) as session:
            stmt = select(Job)
            if status == "active":
                stmt = stmt.where(Job.status != JobStatus.ARCHIVED.value)
            elif status != "all":
                stmt = stmt.where(Job.status == status)
            if min_score is not None:
                stmt = stmt.where(Job.match_score >= min_score)
            jobs = list(session.scalars(stmt).all())
            for j in jobs:
                session.expunge(j)

        # Python 端排序 (salary/education/mode 都是字符串字段, SQL ORDER BY 难弄)
        sort_fn = SORTERS.get(sort, SORTERS["score"])
        jobs.sort(key=sort_fn)

        return render(
            "index.html",
            jobs=jobs,
            min_score=min_score,
            status=status,
            sort=sort,
            total=len(jobs),
        )

    @app.get("/job/{job_id}")
    def job_detail(job_id: int):
        job = _get_job(job_id)
        return render(
            "job_detail.html",
            job=job,
            strengths_list=(job.match_strengths or "").splitlines(),
            fit_bullets=(job.match_fit_bullets or "").splitlines(),
            keywords_list=(job.match_keywords or "").splitlines(),
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

    # ---- Onboarding (首次使用 / 重设画像) ----
    @app.get("/onboarding")
    def onboarding_form():
        existing_desc = load_user_description(config) or ""
        profile = load_profile(config)
        history = list_profile_snapshots(config)
        current_id = get_current_profile_id(config)
        return render(
            "onboarding.html",
            existing_desc=existing_desc,
            has_profile=profile is not None,
            supported_exts=", ".join(sorted(SUPPORTED_EXTS)),
            history=history,
            current_id=current_id,
            pipeline_running=pipeline_state["running"],
        )

    @app.post("/onboarding/submit")
    async def onboarding_submit(
        background_tasks: BackgroundTasks,
        description: str = Form(...),
        resume: UploadFile | None = File(None),
    ):
        # 如果当前描述跟上次完全相同, 不重复跑
        existing = load_user_description(config) or ""
        desc = (description or "").strip()
        if not desc:
            raise HTTPException(400, "请填写求职需求描述")

        if existing.strip() == desc and not (resume and resume.filename):
            # 描述和简历都没变, 直接回主页
            return RedirectResponse(url="/", status_code=303)

        # 如果有流水线在跑, 先中断
        if pipeline_state["running"]:
            print("[onboarding] 检测到流水线在跑, 请求取消...")
            _wait_for_cancel(timeout=60)

        # 1) 如果有新简历, 保存
        resume_dir = config.path("resume_dir")
        uploaded_filename = None
        if resume and resume.filename:
            ext = Path(resume.filename).suffix.lower()
            if ext not in SUPPORTED_EXTS:
                raise HTTPException(
                    400,
                    f"不支持的简历格式 {ext}. 仅支持: {sorted(SUPPORTED_EXTS)}",
                )
            content = await resume.read()
            if not content:
                raise HTTPException(400, "上传文件为空")
            target = resume_dir / resume.filename
            target.write_bytes(content)
            uploaded_filename = resume.filename
            try:
                parse_and_cache(resume_dir)
            except Exception as e:
                raise HTTPException(500, f"解析简历失败: {e}")

        # 2) 保存用户描述
        save_user_description(config, desc)

        # 3) 确认 DB 在
        init_db(config.path("db_path"))

        # 4) 不再阻塞: analyze + collect + match 都丢后台
        background_tasks.add_task(_run_pipeline_bg, True, desc, uploaded_filename)

        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.post("/profiles/{profile_id}/use")
    def use_profile(profile_id: int, background_tasks: BackgroundTasks):
        """切回某历史画像 + 重跑流水线."""
        # 取消当前跑的(如有)
        if pipeline_state["running"]:
            print(f"[use_profile] 流水线在跑, 请求取消以切换到 #{profile_id}...")
            _wait_for_cancel(timeout=60)
        try:
            activate_profile_snapshot(config, profile_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        # do_analyze=False: 直接用已激活的画像跑 collect+match
        background_tasks.add_task(_run_pipeline_bg, False, None, None)
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.post("/pipeline/cancel")
    def cancel_pipeline():
        if not pipeline_state["running"]:
            return RedirectResponse(url="/onboarding/processing", status_code=303)
        pipeline_state["cancel_requested"] = True
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    @app.get("/onboarding/processing")
    def onboarding_processing():
        return render(
            "processing.html",
            state=pipeline_state,
            elapsed=_elapsed(pipeline_state),
        )

    @app.post("/refresh")
    def refresh(background_tasks: BackgroundTasks):
        """主页点 'Refresh' 时手动重跑 collect + match (不重做 analyze)."""
        if pipeline_state["running"]:
            raise HTTPException(409, "已有流水线在跑")
        background_tasks.add_task(_run_pipeline_bg, False, None)
        return RedirectResponse(url="/onboarding/processing", status_code=303)

    return app


def _elapsed(state: dict) -> str:
    if not state.get("started_at"):
        return ""
    end = state.get("ended_at") or datetime.now()
    delta = end - state["started_at"]
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


def _find_free_port(host: str, start: int, max_tries: int = 20) -> int:
    """从 start 开始递增找一个能 bind 上的端口."""
    import socket
    for p in range(start, start + max_tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, p))
            s.close()
            return p
        except OSError:
            s.close()
            continue
    raise RuntimeError(f"在 {start}..{start + max_tries - 1} 范围内找不到可用端口")


def run_server(config: Config, host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    actual_port = _find_free_port(host, port)
    if actual_port != port:
        print(f"⚠️  端口 {port} 被占用,改用 {actual_port}")
    app = create_app(config)
    print(f"\n→ JobHunter web UI: http://{host}:{actual_port}\n")
    uvicorn.run(app, host=host, port=actual_port, log_level="info")
