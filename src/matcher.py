"""岗位匹配评分器.

借鉴 n8n 工作流的"Structured Output Parser"思路: 用 Anthropic tool_use 强制 schema,
比让模型输出 JSON 文本再正则提取靠谱得多 (成功率从 ~95% 提到 ~99.9%).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .agent import load_prompt, make_client, render
from .config import Config
from .db import Job, JobStatus, session_scope


# Tool schema: 强制模型把评分结果以这个结构调用
SCORING_TOOL = {
    "name": "submit_match_score",
    "description": "提交对该岗位与候选人简历的匹配评估结果",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "匹配度评分 0-100",
            },
            "summary": {
                "type": "string",
                "description": "一句话总结匹配情况,中文,不超过 50 字",
            },
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "命中点列表",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "差距点列表",
            },
            "recommend": {
                "type": "boolean",
                "description": "是否值得花时间投递",
            },
        },
        "required": ["score", "summary", "strengths", "gaps", "recommend"],
    },
}


@dataclass
class MatchResult:
    score: float
    summary: str
    strengths: list[str]
    gaps: list[str]
    recommend: bool

    @classmethod
    def from_tool_input(cls, data: dict) -> "MatchResult":
        return cls(
            score=float(data.get("score", 0)),
            summary=str(data.get("summary", "")).strip(),
            strengths=list(data.get("strengths") or []),
            gaps=list(data.get("gaps") or []),
            recommend=bool(data.get("recommend", False)),
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

            job.match_score = result.score
            job.match_summary = result.summary
            job.match_strengths = "\n".join(result.strengths)
            job.match_gaps = "\n".join(result.gaps)
            if result.score < auto_archive_below:
                job.status = JobStatus.ARCHIVED.value
            else:
                job.status = JobStatus.SCORED.value

            session.add(job)
            session.commit()
            results.append((job.id, result))

    return results
