"""手动添加岗位: 粘贴 JD + 个人备注 → LLM 提取结构化字段 → 入库 → 自动评分.

用户输入:
  - jd_text / jd_file : 岗位 JD 原文 (必填)
  - url               : 岗位链接 (选填, 用于去重)
  - user_note         : 个人备注, 例如 "朋友推荐的、公司小但技术好" (选填)

LLM 根据 JD + 备注提取:
  title, company, location, salary, work_mode, min_education, description_clean
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from .agent import make_client
from .config import Config
from .db import Job, JobStatus, session_scope
from .resume_reader import read_resume
from .dedup import content_hash


# ── Tool schema ──────────────────────────────────────────────────────────────

PARSE_JD_TOOL: dict[str, Any] = {
    "name": "submit_parsed_job",
    "description": "从 JD 原文中提取结构化字段，生成可直接入库的岗位记录",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "英文标准岗位名，例如 'Machine Learning Engineer'",
            },
            "company": {
                "type": "string",
                "description": "公司全称",
            },
            "location": {
                "type": "string",
                "description": "工作地点，例如 'San Francisco, CA' 或 'Remote'，不确定填 'Unspecified'",
            },
            "salary": {
                "type": "string",
                "description": "薪资范围，例如 '$150k–$200k'，JD 未提及则留空",
            },
            "work_mode": {
                "type": "string",
                "enum": ["remote", "hybrid", "onsite", "unspecified"],
            },
            "min_education": {
                "type": "string",
                "enum": ["high_school", "bachelor", "master", "phd", "any", "unspecified"],
            },
            "description_clean": {
                "type": "string",
                "description": "清理后的完整 JD 正文：保留职责、技术要求、技术栈全文，去掉公司宣传广告语",
            },
        },
        "required": ["title", "company", "location", "work_mode", "min_education", "description_clean"],
    },
}

PARSE_PROMPT = """\
你是一个结构化信息提取助手。从下面的岗位 JD 中提取字段，调用 submit_parsed_job 工具。

提取规则：
- title: 英文标准岗位名，不要直译中文，用 LinkedIn 上真实存在的 title
- company: 公司全称
- description_clean: 保留 JD 完整正文（职责 + 技术要求 + 技术栈），去掉无关宣传语
- 字段 JD 未明确提及时按 schema 描述填 unspecified 或空字符串
{url_line}
{note_line}
---
{raw_text}
---
"""


# ── 数据结构 ─────────────────────────────────────────────────────────────────

@dataclass
class ManualJobInput:
    raw_text: str                   # JD 原文 (必填)
    url: str = ""                   # 岗位链接 (选填)
    user_note: str = ""             # 个人备注 (选填)
    source_hint: str = "manual"


@dataclass
class ParsedJob:
    title: str
    company: str
    location: str
    salary: str
    work_mode: str
    min_education: str
    description_clean: str


# ── 核心逻辑 ─────────────────────────────────────────────────────────────────

def _parse_jd(config: Config, inp: ManualJobInput) -> ParsedJob:
    """调 LLM 从 JD 原文提取结构化字段。"""
    client, model_name = make_client(config, "matcher")

    url_line  = f"岗位链接：{inp.url}" if inp.url else ""
    note_line = f"用户备注：{inp.user_note}" if inp.user_note else ""

    prompt = PARSE_PROMPT.format(
        url_line=url_line,
        note_line=note_line,
        raw_text=inp.raw_text[:8000],
    )

    resp = client.messages.create(
        model=model_name,
        max_tokens=2048,
        tools=[PARSE_JD_TOOL],
        tool_choice={"type": "tool", "name": "submit_parsed_job"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_parsed_job":
            d = block.input or {}
            return ParsedJob(
                title=str(d.get("title", "Unknown Title")).strip(),
                company=str(d.get("company", "Unknown Company")).strip(),
                location=str(d.get("location", "Unspecified")).strip(),
                salary=str(d.get("salary", "")).strip(),
                work_mode=str(d.get("work_mode", "unspecified")),
                min_education=str(d.get("min_education", "unspecified")),
                description_clean=str(d.get("description_clean", inp.raw_text)).strip(),
            )

    raise RuntimeError("LLM 未返回 submit_parsed_job 工具调用")


def _dedup_check(config: Config, url: str, chash: str) -> Optional[Job]:
    """三层去重: URL → content_hash。"""
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        if url:
            existing = session.scalars(select(Job).where(Job.url == url)).first()
            if existing:
                session.expunge(existing)
                return existing
        existing = session.scalars(select(Job).where(Job.content_hash == chash)).first()
        if existing:
            session.expunge(existing)
            return existing
    return None


def add_job_from_text(
    config: Config,
    inp: ManualJobInput,
    *,
    run_matcher: bool = True,
    profile_id: Optional[int] = None,
) -> tuple[Job, bool]:
    """主入口: 解析 → 去重 → 入库 → 评分。返回 (job, is_new)。"""
    print("[manual_add] 解析 JD...")
    parsed = _parse_jd(config, inp)
    print(f"[manual_add] 解析完成: {parsed.title} @ {parsed.company}")

    url = inp.url.strip()
    chash = content_hash(parsed.title, parsed.company, parsed.location)

    duplicate = _dedup_check(config, url, chash)
    if duplicate:
        print(f"[manual_add] 已存在相同岗位 #{duplicate.id}，跳过")
        return duplicate, False

    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = Job(
            source=inp.source_hint,
            url=url or f"manual://{parsed.company}/{parsed.title}",
            title=parsed.title,
            company=parsed.company,
            location=parsed.location or None,
            salary=parsed.salary or None,
            description=parsed.description_clean,
            work_mode=parsed.work_mode,
            min_education=parsed.min_education,
            status=JobStatus.NEW.value,
            profile_id=profile_id,
            content_hash=chash,
            notes=inp.user_note or None,
        )
        session.add(job)
        session.commit()
        job_id = job.id
        print(f"[manual_add] 已入库 #{job_id}: {parsed.title} @ {parsed.company}")

    if run_matcher:
        try:
            from .matcher import score_job, MatchResult
            from .agent import make_client
            from .resume_reader import load_cached

            resume_text = load_cached(config.path("resume_dir"))
            client, model_name = make_client(config, "matcher")

            with session_scope(db_path) as session:
                job_obj = session.get(Job, job_id)
                result: MatchResult = score_job(client, model_name, config, resume_text, job_obj)

                job_obj.match_score        = result.score
                job_obj.match_summary      = result.summary
                job_obj.match_keywords     = "\n".join(result.keywords)
                job_obj.match_fit_bullets  = "\n".join(result.fit_bullets)
                job_obj.match_connector    = result.connector
                job_obj.match_strengths    = "\n".join(f"- {b}" for b in result.fit_bullets)
                job_obj.score_background   = result.sub_scores.background
                job_obj.score_skills       = result.sub_scores.skills
                job_obj.score_experience   = result.sub_scores.experience
                job_obj.score_seniority    = result.sub_scores.seniority
                job_obj.score_authorization = result.sub_scores.authorization
                job_obj.score_company      = result.sub_scores.company
                job_obj.work_mode          = result.work_mode
                job_obj.min_education      = result.min_education
                job_obj.status             = JobStatus.SCORED.value
                session.add(job_obj)
                session.commit()

            print(f"[manual_add] 评分完成 #{job_id}: {result.score:.0f}分")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[manual_add] 评分失败 (不影响入库): {e}")

    with session_scope(db_path) as session:
        final = session.get(Job, job_id)
        session.expunge(final)
    return final, True


def add_job_from_file(
    config: Config,
    file_path: Path,
    url: str = "",
    user_note: str = "",
    *,
    run_matcher: bool = True,
    profile_id: Optional[int] = None,
) -> tuple[Job, bool]:
    """从文件 (PDF/DOCX/MD/TXT) 读取 JD 后入库。"""
    raw_text = read_resume(file_path)
    inp = ManualJobInput(raw_text=raw_text, url=url, user_note=user_note)
    return add_job_from_text(config, inp, run_matcher=run_matcher, profile_id=profile_id)
