"""Job matching scorer (6-dimension version).

Inspired by DailyJobMatch design:
- 6 sub-dimension scores (background/skills/experience/seniority/work authorization/company type)
- Extract keywords / fit_bullets / connector for cover_letter reuse
- Use Anthropic tool_use to enforce schema, ~99.9% accuracy
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from .agent import load_prompt, make_client, render, _convert_tool_to_openai
from .config import Config
from .db import Job, JobStatus, session_scope


SCORING_TOOL = {
    "name": "submit_match_score",
    "description": "Submit 6-dimension match assessment results for this job vs. candidate resume",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "object",
                "description": "6 sub-dimension scores, sub-scores sum to overall",
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
                "description": "One-sentence match summary, max 50 words",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key tech/concepts from JD, 5-10 items, for ATS optimization",
            },
            "fit_bullets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-5 bullet points why candidate fits this job, English, will be reused in cover letter",
            },
            "connector": {
                "type": "string",
                "description": "Specific connection between candidate and company, one-sentence English, will be cover letter hook",
            },
            "recommend": {
                "type": "boolean",
                "description": "Recommend applying",
            },
            "work_mode": {
                "type": "string",
                "enum": ["remote", "hybrid", "onsite", "unspecified"],
                "description": "Work mode described in JD. Use unspecified if JD doesn't say",
            },
            "min_education": {
                "type": "string",
                "enum": ["high_school", "bachelor", "master", "phd", "any", "unspecified"],
                "description": "Minimum education required by JD. Use unspecified if JD doesn't say",
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
    config: Config,
    resume_text: str,
    job: Job,
) -> MatchResult:
    """Score a job using the matcher role (which uses tools)."""
    from .agent import make_client

    client, model_name, provider = make_client(config, "matcher")
    template = load_prompt("matcher")
    prompt = render(
        template,
        preferences=json.dumps(config.preferences, ensure_ascii=False, indent=2),
        resume=resume_text,
        title=job.title,
        company=job.company,
        location=job.location or "(unspecified)",
        description=job.description or "(no JD)",
    )

    if provider == "deepseek":
        # OpenAI format: convert tool schema and use streaming
        tools = [_convert_tool_to_openai(SCORING_TOOL)]
        with client.chat.completions.stream(
            model=model_name,
            max_tokens=config.max_tokens,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_match_score"}},
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        ) as stream:
            resp = stream.get_final_completion()
        if resp.choices[0].message.tool_calls:
            tool_call = resp.choices[0].message.tool_calls[0]
            return MatchResult.from_tool_input(json.loads(tool_call.function.arguments))
        raise RuntimeError("Model did not return submit_match_score tool call")
    else:
        # Anthropic format: use native tool_use
        with client.messages.stream(
            model=model_name,
            max_tokens=config.max_tokens,
            tools=[SCORING_TOOL],
            tool_choice={"type": "tool", "name": "submit_match_score"},
            messages=[{"role": "user", "content": prompt}],
        ) as _s:
            resp = _s.get_final_message()
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_match_score":
                return MatchResult.from_tool_input(block.input)
        raise RuntimeError("Model did not return submit_match_score tool call")




def _legacy_strengths_text(fit_bullets: list[str]) -> str:
    """Legacy field match_strengths filled with fit_bullets, backward compatible."""
    return "\n".join(f"- {b}" for b in fit_bullets) if fit_bullets else ""


def score_pending(
    config: Config,
    resume_text: str,
    *,
    limit: int | None = None,
    should_continue=None,
    on_scored=None,   # callback(job_id: int, score: float) called after each job
) -> list[tuple[int, MatchResult]]:
    if should_continue is None:
        should_continue = lambda: True
    db_path = config.path("db_path")
    auto_archive_below = float(config.scoring.get("auto_archive_below", 40))
    results: list[tuple[int, MatchResult]] = []

    with session_scope(db_path) as session:
        stmt = select(Job).where(Job.status == JobStatus.NEW.value)
        if limit:
            stmt = stmt.limit(limit)
        jobs = session.scalars(stmt).all()

        for job in jobs:
            if not should_continue():
                print("[match] Cancel signal detected, exiting early")
                break
            try:
                result = score_job(config, resume_text, job)
            except Exception as e:
                print(f"[!] Scoring failed for #{job.id} {job.title} @ {job.company}: {e}")
                continue

            # Write overall score + sub-scores + new fields
            job.match_score = result.score
            job.match_summary = result.summary
            job.match_strengths = _legacy_strengths_text(result.fit_bullets)
            # match_gaps no longer directly given by model, can infer from sub-scores (optional, leave empty for now)

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
            if on_scored:
                try:
                    on_scored(job.id, result.score)
                except Exception:
                    pass

    return results
