"""岗位匹配评分器 (6 维度版).

借鉴 DailyJobMatch 的设计:
- 6 个子维度评分(背景/技能/经验/资历/工作授权/公司类型)
- 顺手提取 keywords / fit_bullets / connector,供 cover_letter 复用
- 用 Anthropic tool_use 强制 schema,准确率 ~99.9%
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from .agent import load_prompt, make_client, render
from .config import Config
from .db import Job, JobStatus, session_scope


SCORING_TOOL = {
    "name": "submit_match_score",
    "description": "提交对该岗位与候选人简历的 6 维度匹配评估结果",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "object",
                "description": "6 维度子评分,子分相加等于 overall",
                "properties": {
                    "background_match":     {"type": "integer", "minimum": 0, "maximum": 10},
                    "skills_overlap":       {"type": "integer", "minimum": 0, "maximum": 30},
                    "experience_relevance": {"type": "integer", "minimum": 0, "maximum": 30},
                    "seniority":            {"type": "integer", "minimum": 0, "maximum": 10},
                    "authorization":        {"type": "integer", "minimum": 0, "maximum": 10},
                    "company_score":        {"type": "integer", "minimum": 0, "maximum": 10},
                    "overall":              {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": [
                    "background_match", "skills_overlap", "experience_relevance",
                    "seniority", "authorization", "company_score", "overall",
                ],
            },
            "summary": {
                "type": "string",
                "description": "一句话总结匹配情况,中文,不超过 50 字",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "JD 里的关键技术/概念,5-10 个,用于 ATS 优化",
            },
            "fit_bullets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "为什么候选人 fit 这岗位的 3-5 条子弹点,英文,会复用到求职信",
            },
            "connector": {
                "type": "string",
                "description": "候选人和这家公司的具体连接点,一句话英文,会作为求职信钩子",
            },
            "recommend": {
                "type": "boolean",
                "description": "是否推荐投递",
            },
            "work_mode": {
                "type": "string",
                "enum": ["remote", "hybrid", "onsite", "unspecified"],
                "description": "JD 中描述的工作模式. JD 未明说就 unspecified",
            },
            "min_education": {
                "type": "string",
                "enum": ["high_school", "bachelor", "master", "phd", "any", "unspecified"],
                "description": "JD 要求的最低学历. JD 未明说就 unspecified",
            },
        },
        "required": ["score", "summary", "keywords", "fit_bullets", "connector", "recommend", "work_mode", "min_education"],
    },
}


@dataclass
class SubScores:
    background: float = 0
    skills: float = 0
    experience: float = 0
    seniority: float = 0
    authorization: float = 0
    company: float = 0
    overall: float = 0

    @classmethod
    def from_dict(cls, d: dict) -> "SubScores":
        return cls(
            background=float(d.get("background_match", 0)),
            skills=float(d.get("skills_overlap", 0)),
            experience=float(d.get("experience_relevance", 0)),
            seniority=float(d.get("seniority", 0)),
            authorization=float(d.get("authorization", 0)),
            company=float(d.get("company_score", 0)),
            overall=float(d.get("overall", 0)),
        )


@dataclass
class MatchResult:
    score: float                # overall
    sub_scores: SubScores
    summary: str
    keywords: list[str] = field(default_factory=list)
    fit_bullets: list[str] = field(default_factory=list)
    connector: str = ""
    recommend: bool = False
    work_mode: str = "unspecified"
    min_education: str = "unspecified"

    @classmethod
    def from_tool_input(cls, data: dict) -> "MatchResult":
        sub = SubScores.from_dict(data.get("score") or {})
        return cls(
            score=sub.overall,
            sub_scores=sub,
            summary=str(data.get("summary", "")).strip(),
            keywords=list(data.get("keywords") or []),
            fit_bullets=list(data.get("fit_bullets") or []),
            connector=str(data.get("connector", "")).strip(),
            recommend=bool(data.get("recommend", False)),
            work_mode=str(data.get("work_mode", "unspecified")),
            min_education=str(data.get("min_education", "unspecified")),
        )


def score_job(
    client,
    model_name: str,
    config: Config,
    resume_text: str,
    job: Job,
) -> MatchResult:
    template = load_prompt("matcher")
    prompt = render(
        template,
        preferences=json.dumps(config.preferences, ensure_ascii=False, indent=2),
        resume=resume_text,
        title=job.title,
        company=job.company,
        location=job.location or "(未指定)",
        description=job.description or "(无 JD)",
    )
    resp = client.messages.create(
        model=model_name,
        max_tokens=config.max_tokens,
        tools=[SCORING_TOOL],
        tool_choice={"type": "tool", "name": "submit_match_score"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_match_score":
            return MatchResult.from_tool_input(block.input)
    raise RuntimeError("模型没返回 submit_match_score 工具调用")


def _legacy_strengths_text(fit_bullets: list[str]) -> str:
    """老字段 match_strengths 用 fit_bullets 填充,向后兼容."""
    return "\n".join(f"- {b}" for b in fit_bullets) if fit_bullets else ""


def score_pending(
    config: Config,
    resume_text: str,
    *,
    limit: int | None = None,
) -> list[tuple[int, MatchResult]]:
    client, model_name = make_client(config, "matcher")
    db_path = config.path("db_path")
    auto_archive_below = float(config.scoring.get("auto_archive_below", 40))
    results: list[tuple[int, MatchResult]] = []

    with session_scope(db_path) as session:
        stmt = select(Job).where(Job.status == JobStatus.NEW.value)
        if limit:
            stmt = stmt.limit(limit)
        jobs = session.scalars(stmt).all()

        for job in jobs:
            try:
                result = score_job(client, model_name, config, resume_text, job)
            except Exception as e:
                print(f"[!] 给 #{job.id} {job.title} @ {job.company} 评分失败: {e}")
                continue

            # 写入总分 + 子分 + 新字段
            job.match_score = result.score
            job.match_summary = result.summary
            job.match_strengths = _legacy_strengths_text(result.fit_bullets)
            # match_gaps 不再由模型直接给,可以从 sub-scores 倒推(可选,先留空)

            job.score_background = result.sub_scores.background
            job.score_skills = result.sub_scores.skills
            job.score_experience = result.sub_scores.experience
            job.score_seniority = result.sub_scores.seniority
            job.score_authorization = result.sub_scores.authorization
            job.score_company = result.sub_scores.company

            job.match_keywords = "\n".join(result.keywords)
            job.match_fit_bullets = "\n".join(result.fit_bullets)
            job.match_connector = result.connector
            job.work_mode = result.work_mode
            job.min_education = result.min_education

            if result.score < auto_archive_below:
                job.status = JobStatus.ARCHIVED.value
            else:
                job.status = JobStatus.SCORED.value

            session.add(job)
            session.commit()
            results.append((job.id, result))

    return results
