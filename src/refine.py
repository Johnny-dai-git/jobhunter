"""Resume conversational refinement module.

Workflow:
  1. User sends modification request (natural language)
  2. System automatically injects professional HR/HM prompt + current resume + conversation history
  3. Claude Opus returns updated complete resume + change notes
  4. New version stored in ResumeRevision table
  5. Conversation history persisted to JSON file for next session

Each user message automatically appends the following system context:
  - HR perspective rules
  - HM perspective rules
  - Format enforcement requirements
  - Current resume full text
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from .agent import make_client
from .config import Config
from .db import Job, ResumeRevision, session_scope
from .pdf_generator import md_to_pdf


# ── System Prompt (injected in each conversation turn) ─────────────────────────────────────────────

REFINE_SYSTEM_PROMPT = """\
You are a top-tier resume refinement consultant with two professional perspectives:

**HR Screening Perspective (6-second scan)**
- ATS keyword coverage: do core technical terms for target role appear in resume
- Format clarity: heading hierarchy, bullet length, whitespace rhythm
- Above-the-fold impact: are Summary + Skills in the most prominent position
- Quantification density: are numbers and metrics dense enough

**HM Technical Depth Perspective**
- Technical credibility of each project: do descriptions reflect real system design ability
- Independent delivery proof: is there evidence of "I designed/I built/I independently"
- Differentiated value: what does this candidate have that most peers don't
- Results-difficulty match: does technical difficulty behind numbers show through

---

**Every modification must strictly follow these rules:**

1. **Don't fabricate anything** — only rewrite, reorder, rephrase existing experiences
2. **Maintain original format** — strictly preserve original resume section structure, heading hierarchy, typography style and bullet format. Only change wording and content emphasis, don't redesign layout, don't add sections that weren't originally there
3. **Preserve all hyperlinks** — GitHub, Portfolio, LinkedIn, paper links, etc. retain as-is, don't lose any
4. **Bold key terms** — in each bullet, mark the most core technical terms or quantified outcomes with `**term**`, no more than 2 per bullet
5. **No metadata** — resume must not show "Tailored for", "for position", "modified version" etc.
6. **Output complete resume** — wrap in ```markdown, output is a complete resume ready to send to HR, not fragments
7. **Change notes** — after ```markdown fence attach a short note (≤100 chars): what changed, why this helps HR/HM scoring

---

Current target position: **{title} @ {company}**
"""

REFINE_USER_TEMPLATE = """\
{user_message}

---
Current resume (please modify based on this):

```markdown
{current_resume}
```
"""


# ── Conversation History Management ──────────────────────────────────────

def _chat_path(config: Config, job_id: int) -> Path:
    outputs_dir = config.path("outputs_dir")
    return outputs_dir / f"{job_id:03d}_chat.json"


def _migrate_message(msg: dict) -> dict:
    """Backward compatibility: old version embedded complete resume in user message, new version only stores original user request.
    Detection method: if user content has '---\nCurrent resume' separator → extract part before separator.
    """
    if msg.get("role") == "user":
        content = msg.get("content", "")
        sep = "---\nCurrent resume"
        if sep in content:
            return {"role": "user", "content": content.split(sep)[0].strip()}
    return msg


def load_chat_history(config: Config, job_id: int) -> list[dict]:
    path = _chat_path(config, job_id)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # Backward compatibility: strip resume content embedded in user message
        migrated = [_migrate_message(m) for m in raw]
        # If changes made, overwrite in place to avoid re-migration next time
        if migrated != raw:
            path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
        return migrated
    except Exception:
        return []


def save_chat_history(config: Config, job_id: int, messages: list[dict]) -> None:
    path = _chat_path(config, job_id)
    path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_chat_history(config: Config, job_id: int) -> None:
    path = _chat_path(config, job_id)
    if path.exists():
        path.unlink()


# ── Version Management ─────────────────────────────────────────────────────────────────

def get_revisions(config: Config, job_id: int) -> list[ResumeRevision]:
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        rows = list(session.scalars(
            select(ResumeRevision)
            .where(ResumeRevision.job_id == job_id)
            .order_by(ResumeRevision.version_num.desc())
        ).all())
        for r in rows:
            session.expunge(r)
        return rows


def get_revision(config: Config, job_id: int, version_num: int) -> Optional[ResumeRevision]:
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        row = session.scalars(
            select(ResumeRevision)
            .where(ResumeRevision.job_id == job_id, ResumeRevision.version_num == version_num)
        ).first()
        if row:
            session.expunge(row)
        return row


def save_revision(config: Config, job_id: int, md_content: str, note: str = "") -> ResumeRevision:
    """Save a new version, version number auto-increments."""
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        last = session.scalars(
            select(ResumeRevision)
            .where(ResumeRevision.job_id == job_id)
            .order_by(ResumeRevision.version_num.desc())
        ).first()
        next_num = (last.version_num + 1) if last else 1

        rev = ResumeRevision(
            job_id=job_id,
            version_num=next_num,
            md_content=md_content,
            note=note or f"Version {next_num}",
        )
        session.add(rev)
        session.commit()
        session.refresh(rev)   # After commit expires, refresh reloads id/version_num and other fields
        session.expunge(rev)   # Then detach so accessing attributes is safe
        return rev


def get_current_resume_md(config: Config, job_id: int) -> Optional[str]:
    """Get latest version resume markdown, if no versions then read original tailor output."""
    revisions = get_revisions(config, job_id)
    if revisions:
        return revisions[0].md_content  # Ordered by version_num desc, first is latest

    # Fallback: read original tailor output md file
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if job and job.tailored_resume_path:
            path = Path(job.tailored_resume_path)
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                # Extract ```markdown ... ``` body
                m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", raw, re.DOTALL)
                return m.group(1).strip() if m else raw.strip()
    return None


# ── Core Conversation Functions ─────────────────────────────────────────────────────────────

def _extract_md_and_note(text: str) -> tuple[str, str]:
    """Extract markdown body and modification notes from Claude output."""
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        md_body = m.group(1).strip()
        # Text outside the fence is the modification note
        note = text[m.end():].strip()
        if not note:
            note = text[:m.start()].strip()
        return md_body, note[:300]
    return text.strip(), ""


def chat_refine(
    config: Config,
    job_id: int,
    user_message: str,
    auto_save: bool = True,
) -> dict:
    """Send a conversation message, Claude returns updated resume.

    Returns:
      {
        "md_content": str,      # New resume markdown
        "note": str,            # Claude's change notes
        "version_num": int,     # Saved version number (when auto_save=True)
        "messages": list[dict], # Complete conversation history (for frontend update)
      }
    """
    from .agent import llm_complete

    db_path = config.path("db_path")

    # Get job information
    with session_scope(db_path) as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} does not exist")
        title, company = job.title, job.company

    # Get current resume
    current_resume = get_current_resume_md(config, job_id)
    if not current_resume:
        raise ValueError("Customized resume not yet generated, please click 'Generate Customized Resume' first")

    # Load conversation history (only stores user original request + assistant change notes, not complete messages with embedded resume)
    history = load_chat_history(config, job_id)   # [{role, content}] compact version

    # Append current round user request (only original text, no resume)
    history.append({"role": "user", "content": user_message})

    # Build complete messages for API: history + last message with current resume injected
    # Only last user message needs complete resume, history only keeps conversation summary
    api_messages: list[dict] = []
    for i, msg in enumerate(history):
        if msg["role"] == "user":
            is_last = (i == len(history) - 1)
            if is_last:
                # Last message injects latest resume
                api_messages.append({"role": "user", "content": REFINE_USER_TEMPLATE.format(
                    user_message=msg["content"],
                    current_resume=current_resume,
                )})
            else:
                # Historical user messages only keep original request (short), avoid context explosion
                api_messages.append({"role": "user", "content": msg["content"]})
        else:
            api_messages.append(msg)

    # Call Claude Opus
    client, model_name, provider = make_client(config, "tailor")
    system_prompt = REFINE_SYSTEM_PROMPT.format(title=title, company=company)

    assistant_text = llm_complete(
        client,
        model_name,
        provider,
        messages=api_messages,
        max_tokens=4096,
        system=system_prompt,
    )

    # Parse output (assistant_text is already a string from llm_complete)
    md_content, note = _extract_md_and_note(assistant_text)

    # Only store assistant's change notes (not complete resume), keep history compact
    history.append({"role": "assistant", "content": note or "(Resume updated)"})
    save_chat_history(config, job_id, history)

    # Save version
    version_num = None
    if auto_save and md_content:
        rev = save_revision(config, job_id, md_content, note=note)
        version_num = rev.version_num

        # Also update PDF
        try:
            outputs_dir = config.path("outputs_dir")
            with session_scope(db_path) as session:
                job = session.get(Job, job_id)
                if job:
                    safe_company = re.sub(r"[^\w\-]+", "_", company)[:40]
                    safe_title   = re.sub(r"[^\w\-]+", "_", title)[:40]
                    pdf_path = outputs_dir / f"{job_id:03d}_{safe_company}_{safe_title}_v{version_num}.pdf"
                    md_to_pdf(md_content, pdf_path)
                    # Update job's latest pdf path
                    job.tailored_resume_pdf_path = str(pdf_path)
                    session.add(job)
                    session.commit()
        except Exception as e:
            print(f"[refine] PDF generation failed: {e}")

    # history is already compact version (user=original request, assistant=change notes), return directly
    return {
        "md_content": md_content,
        "note": note,
        "version_num": version_num,
        "messages": history,  # Already in clean compact format
    }
