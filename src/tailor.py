"""Resume tailor — two-round pipeline.

Round 1 (DeepSeek, cheap & fast):
    JD + resume + materials → gap analysis + specific modification plan

Round 2 (Claude Opus, high quality):
    plan + resume + materials → final tailored resume

Output 2 files:
    {id:03d}_{company}_{title}_resume.md   (source, editable)
    {id:03d}_{company}_{title}_resume.pdf  (for submission)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .agent import ClaudeClient, load_prompt, make_client, render
from .config import Config
from .db import Job, session_scope
from .pdf_generator import md_to_pdf
from .resume_reader import read_materials


# ── Round 1 Tool Schema (DeepSeek generates plan) ────────────────────────────────

RESUME_PLAN_TOOL: dict[str, Any] = {
    "name": "submit_resume_plan",
    "description": "Submit resume modification plan: gap analysis + specific executable instructions",
    "input_schema": {
        "type": "object",
        "properties": {
            "gap_analysis": {
                "type": "object",
                "description": "Gap analysis between JD and resume",
                "properties": {
                    "jd_keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Frequently appearing tech terms/verbs/metrics in JD, these are ATS core",
                    },
                    "strong_matches": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Experiences/skills in candidate's resume that strongly match JD, keep and highlight directly",
                    },
                    "gaps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required by JD but clearly insufficient or missing in resume (list only real gaps, don't fabricate)",
                    },
                    "hidden_strengths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Technical depth in materials but not fully shown in resume, can be added",
                    },
                },
                "required": ["jd_keywords", "strong_matches", "gaps"],
            },
            "plan": {
                "type": "object",
                "description": "Specific modification instructions",
                "properties": {
                    "summary_rewrite": {
                        "type": "string",
                        "description": "Summary section rewrite direction: what to emphasize, which JD keywords to use, how to position first sentence",
                    },
                    "skills_priority": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Skills sections arranged by JD priority, format: 'Group name: skill1, skill2'",
                    },
                    "experience_adjustments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "section": {"type": "string", "description": "Which project/experience"},
                                "action": {"type": "string", "enum": ["emphasize", "rewrite", "deprioritize", "add_from_materials"]},
                                "instruction": {"type": "string", "description": "How to modify specifically, reference JD keywords"},
                            },
                            "required": ["section", "action", "instruction"],
                        },
                        "description": "Specific adjustment instructions per experience/project",
                    },
                    "sections_order": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Final resume section order, e.g. ['Summary','Skills','Experience','Projects','Education','Publications']",
                    },
                    "keywords_to_inject": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "JD keywords that must appear in resume (select only those candidate actually used)",
                    },
                },
                "required": ["summary_rewrite", "skills_priority", "experience_adjustments", "keywords_to_inject"],
            },
        },
        "required": ["gap_analysis", "plan"],
    },
}


# ── Core functions ─────────────────────────────────────────────────────────────

def _extract_md_section(text: str) -> str:
    """Extract body wrapped in ```markdown ... ``` from model output."""
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _round1_plan(
    config: Config,
    resume_text: str,
    job: Job,
    extras_text: str,
) -> str:
    """Round 1: DeepSeek analyzes gaps, generates modification plan, return plan JSON string."""
    client, model_name, provider = make_client(config, "matcher")  # matcher → DeepSeek

    prompt = render(
        load_prompt("tailor_plan"),
        resume=resume_text,
        title=job.title,
        company=job.company,
        description=job.description or "(no JD)",
        extras=extras_text or "(no additional materials)",
    )

    print(f"[tailor] Round 1 — {provider} analyzing gaps and generating plan...")

    if provider == "deepseek":
        # OpenAI format
        from .agent import _convert_tool_to_openai
        tools = [_convert_tool_to_openai(RESUME_PLAN_TOOL)]
        with client.chat.completions.stream(
            model=model_name,
            max_tokens=4096,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_resume_plan"}},
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        ) as stream:
            resp = stream.get_final_completion()
        if resp.choices[0].message.tool_calls:
            tool_call = resp.choices[0].message.tool_calls[0]
            plan_data = json.loads(tool_call.function.arguments)
            return _plan_to_text(plan_data)
        raise RuntimeError("[tailor] Round 1 failed: model did not return submit_resume_plan")
    else:
        # Anthropic format
        with client.messages.stream(
            model=model_name,
            max_tokens=4096,
            tools=[RESUME_PLAN_TOOL],
            tool_choice={"type": "tool", "name": "submit_resume_plan"},
            messages=[{"role": "user", "content": prompt}],
        ) as _s:
            resp = _s.get_final_message()

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_resume_plan":
                plan_data = block.input
                # Convert to readable text to pass to Round 2
                return _plan_to_text(plan_data)

        raise RuntimeError("[tailor] Round 1 failed: model did not return submit_resume_plan")


def _plan_to_text(plan_data: dict) -> str:
    """Convert structured plan to readable text for Claude to execute in Round 2."""
    gap = plan_data.get("gap_analysis", {})
    plan = plan_data.get("plan", {})

    lines = ["## Gap Analysis\n"]

    if gap.get("jd_keywords"):
        lines.append("**Core JD Keywords** (must appear in resume):")
        lines.append(", ".join(gap["jd_keywords"]))

    if gap.get("strong_matches"):
        lines.append("\n**Strong Matches** (keep and highlight):")
        for item in gap["strong_matches"]:
            lines.append(f"- {item}")

    if gap.get("gaps"):
        lines.append("\n**Gap Items** (package if possible, skip if not):")
        for item in gap["gaps"]:
            lines.append(f"- {item}")

    if gap.get("hidden_strengths"):
        lines.append("\n**Hidden Strengths in Materials** (can add to bullets):")
        for item in gap["hidden_strengths"]:
            lines.append(f"- {item}")

    lines.append("\n## Modification Instructions\n")

    if plan.get("summary_rewrite"):
        lines.append(f"**Summary Rewrite Direction**: {plan['summary_rewrite']}")

    if plan.get("skills_priority"):
        lines.append("\n**Skills Priority Arrangement**:")
        for s in plan["skills_priority"]:
            lines.append(f"- {s}")

    if plan.get("experience_adjustments"):
        lines.append("\n**Adjustments per Experience/Project**:")
        for adj in plan["experience_adjustments"]:
            action_map = {
                "emphasize": "UP Emphasize",
                "rewrite": "EDIT Rewrite",
                "deprioritize": "DOWN Deprioritize",
                "add_from_materials": "ADD From materials",
            }
            action_label = action_map.get(adj.get("action", ""), adj.get("action", ""))
            lines.append(f"- [{action_label}] **{adj.get('section', '')}**: {adj.get('instruction', '')}")

    if plan.get("keywords_to_inject"):
        lines.append(f"\n**Keywords to Inject**: {', '.join(plan['keywords_to_inject'])}")

    if plan.get("sections_order"):
        lines.append(f"\n**Section Order**: {' → '.join(plan['sections_order'])}")

    return "\n".join(lines)


def tailor_for_job(
    config: Config,
    resume_text: str,
    job_id: int,
    candidate_name: str = "Candidate",
) -> Path:
    """Two-round pipeline generates tailored resume: .md + .pdf. Return .md path."""
    db_path = config.path("db_path")
    outputs_dir = config.path("outputs_dir")
    extras_text = read_materials(config.path("materials_dir"))

    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job id={job_id} does not exist")

        # ── Round 1: DeepSeek → plan ─────────────────────────────────────────
        try:
            plan_text = _round1_plan(config, resume_text, job, extras_text or "")
            print(f"[tailor] Round 1 complete, plan length: {len(plan_text)} characters")
        except Exception as e:
            print(f"[tailor] Round 1 failed: {e}")
            raise RuntimeError(f"Resume tailor Round 1 failed (DeepSeek analysis phase): {str(e)[:120]}") from e

        # ── Round 2: Claude Opus → final resume ──────────────────────────────────
        print(f"[tailor] Round 2 — Claude Opus executing plan to rewrite resume...")
        client = ClaudeClient(config)
        prompt = render(
            load_prompt("tailor"),
            plan=plan_text,
            resume=resume_text,
            extras=extras_text or "(no additional materials)",
            candidate_name=candidate_name,
        )
        # Resume rewrite output can be long (complete markdown + detailed project bullets), allocate token space
        text = client.complete("tailor", prompt, max_tokens=8000)
        md_body = _extract_md_section(text)
        print(f"[tailor] Round 2 complete, resume length: {len(md_body)} characters")

        # ── Write files ────────────────────────────────────────────────────────────
        safe_company = re.sub(r"[^\w\-]+", "_", job.company)[:40]
        safe_title   = re.sub(r"[^\w\-]+", "_", job.title)[:40]
        base = f"{job.id:03d}_{safe_company}_{safe_title}_resume"

        md_path  = outputs_dir / f"{base}.md"
        pdf_path = outputs_dir / f"{base}.pdf"

        # md file saves complete output (with rewrite notes) for easy review
        full_output = f"<!-- Plan -->\n<!--\n{plan_text}\n-->\n\n{text}"
        md_path.write_text(full_output, encoding="utf-8")

        try:
            md_to_pdf(md_body, pdf_path)
        except Exception as e:
            print(f"[tailor] PDF generation failed ({e}), keeping only .md")
            pdf_path = None

        job.tailored_resume_path = str(md_path)
        if pdf_path:
            job.tailored_resume_pdf_path = str(pdf_path)
        session.add(job)
        session.commit()

    return md_path
