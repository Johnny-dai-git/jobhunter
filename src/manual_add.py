"""Manually add job: paste JD + personal notes → LLM extracts structured fields → store → auto-score.

User input:
  - jd_text / jd_file : Job JD raw text (required)
  - url               : Job link (optional, for dedup)
  - user_note         : Personal note, e.g. "friend referred, small company but good tech" (optional)

LLM extracts from JD + notes:
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
    "description": "Extract structured fields from raw JD, generate job record ready to store",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "English standard job title, e.g. 'Machine Learning Engineer'",
            },
            "company": {
                "type": "string",
                "description": "Full company name",
            },
            "location": {
                "type": "string",
                "description": "Work location, e.g. 'San Francisco, CA' or 'Remote', use 'Unspecified' if unsure",
            },
            "salary": {
                "type": "string",
                "description": "Salary range, e.g. '$150k–$200k', leave empty if JD doesn't mention",
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
                "description": "Cleaned complete JD body: keep responsibilities, tech requirements, full tech stack, remove company marketing copy",
            },
        },
        "required": ["title", "company", "location", "work_mode", "min_education", "description_clean"],
    },
}

PARSE_PROMPT = """\
You are a structured information extraction assistant. Extract fields from the job JD below, call submit_parsed_job tool.

Extraction rules:
- title: English standard job title, don't translate directly from Chinese, use title that actually exists on LinkedIn
- company: Full company name
- description_clean: Keep complete JD body (responsibilities + tech requirements + tech stack), remove irrelevant marketing copy
- For fields not explicitly mentioned in JD, fill with unspecified or empty string per schema
{url_line}
{note_line}
---
{raw_text}
---
"""


# ── Data structures ─────────────────────────────────────────────────────────────────

@dataclass
class ManualJobInput:
    raw_text: str                   # Raw JD text (required)
    url: str = ""                   # Job link (optional)
    user_note: str = ""             # Personal note (optional)
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


# ── Core logic ─────────────────────────────────────────────────────────────────

def _parse_jd(config: Config, inp: ManualJobInput) -> ParsedJob:
    """Call LLM to extract structured fields from raw JD."""
    import json
    client, model_name, provider = make_client(config, "matcher")

    url_line  = f"Job URL: {inp.url}" if inp.url else ""
    note_line = f"User note: {inp.user_note}" if inp.user_note else ""

    prompt = PARSE_PROMPT.format(
        url_line=url_line,
        note_line=note_line,
        raw_text=inp.raw_text[:8000],
    )

    if provider == "deepseek":
        # OpenAI format
        from .agent import _convert_tool_to_openai
        tools = [_convert_tool_to_openai(PARSE_JD_TOOL)]
        with client.chat.completions.stream(
            model=model_name,
            max_tokens=2048,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_parsed_job"}},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            resp = stream.get_final_completion()
        if resp.choices[0].message.tool_calls:
            tool_call = resp.choices[0].message.tool_calls[0]
            d = json.loads(tool_call.function.arguments)
            return ParsedJob(
                title=str(d.get("title", "Unknown Title")).strip(),
                company=str(d.get("company", "Unknown Company")).strip(),
                location=str(d.get("location", "Unspecified")).strip(),
                salary=str(d.get("salary", "")).strip(),
                work_mode=str(d.get("work_mode", "unspecified")),
                min_education=str(d.get("min_education", "unspecified")),
                description_clean=str(d.get("description_clean", inp.raw_text)).strip(),
            )
        raise RuntimeError("LLM did not return submit_parsed_job tool call")
    else:
        # Anthropic format
        with client.messages.stream(
            model=model_name,
            max_tokens=2048,
            tools=[PARSE_JD_TOOL],
            tool_choice={"type": "tool", "name": "submit_parsed_job"},
            messages=[{"role": "user", "content": prompt}],
        ) as _s:
            resp = _s.get_final_message()

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

        raise RuntimeError("LLM did not return submit_parsed_job tool call")


def _dedup_check(config: Config, url: str, chash: str) -> Optional[Job]:
    """Three-layer dedup: URL → content_hash."""
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
    """Main entry: parse → dedup → store → score. Return (job, is_new)."""
    print("[manual_add] Parsing JD...")
    parsed = _parse_jd(config, inp)
    print(f"[manual_add] Parse complete: {parsed.title} @ {parsed.company}")

    url = inp.url.strip()
    chash = content_hash(parsed.title, parsed.company, parsed.location)

    duplicate = _dedup_check(config, url, chash)
    if duplicate:
        print(f"[manual_add] Same job already exists #{duplicate.id}, skipping")
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
        print(f"[manual_add] Stored #{job_id}: {parsed.title} @ {parsed.company}")

    if run_matcher:
        try:
            from .matcher import score_job, MatchResult
            from .resume_reader import load_cached

            resume_text = load_cached(config.path("resume_dir"))

            with session_scope(db_path) as session:
                job_obj = session.get(Job, job_id)
                result: MatchResult = score_job(config, resume_text, job_obj)

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

            print(f"[manual_add] Scoring complete #{job_id}: {result.score:.0f} points")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[manual_add] Scoring failed (doesn't affect storage): {e}")

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
    """Read JD from file (PDF/DOCX/MD/TXT) then store."""
    raw_text = read_resume(file_path)
    inp = ManualJobInput(raw_text=raw_text, url=url, user_note=user_note)
    return add_job_from_text(config, inp, run_matcher=run_matcher, profile_id=profile_id)
