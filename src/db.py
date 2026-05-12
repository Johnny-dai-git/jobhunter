"""SQLite database: job table + application tracking."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


class JobStatus(str, Enum):
    NEW = "new"                       # Newly collected, not yet scored
    SCORED = "scored"                 # Scored
    SHORTLISTED = "shortlisted"       # Shortlisted, preparing to apply
    APPLIED = "applied"               # Applied, waiting for response
    PHONE_SCREEN = "phone_screen"     # Phone/initial screening interview
    HR_INTERVIEW = "hr_interview"     # HR interview
    HM_INTERVIEW = "hm_interview"     # Hiring Manager interview
    FINAL_ROUND = "final_round"       # Final round / onsite
    OFFER = "offer"                   # Received offer
    REJECTED = "rejected"             # Rejected
    ARCHIVED = "archived"             # Auto or manually archived
    # Backward compatibility
    INTERVIEW = "interview"           # Old status, unified display as hr_interview


class Profile(Base):
    """Profile snapshot: one row per onboarding submission. History is rollback-able."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    label: Mapped[str] = mapped_column(String(120))                # Auto-extracted first 60 chars from description
    user_description: Mapped[str] = mapped_column(Text)            # User's original text
    resume_filename: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    profile_json: Mapped[str] = mapped_column(Text)                # ProfileAnalysis serialized
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)  # Current active profile
    # Per-profile independent automation settings
    schedule_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)          # Run every N hours, 0=disabled
    enabled_platforms: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # JSON list, e.g. '["linkedin","yc"]'
    job_types_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)             # JSON list, e.g. '["Full-time","Internship"]'


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))            # linkedin / indeed / manual
    external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    profile_id: Mapped[Optional[int]] = mapped_column(ForeignKey("profiles.id"), nullable=True)
    url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(256))
    company: Mapped[str] = mapped_column(String(256))
    location: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # Cross-platform semantic dedup: sha256(normalize(company)|normalize(title)|normalize(location))[:16]
    content_hash: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    salary: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Actual job posting date (provided by platform actors, if available)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Job type — set at collection time from platform API or title inference
    job_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # full-time / internship / contract / part-time

    # Extra structured fields extracted from JD (filled during matcher phase)
    work_mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)   # remote / hybrid / onsite / unspecified
    min_education: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # high_school / bachelor / master / phd / any / unspecified

    # Scoring fields — overall score
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    match_strengths: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    match_gaps: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 6-dimension sub-scores (inspired by DailyJobMatch)
    score_background: Mapped[Optional[float]] = mapped_column(Float, nullable=True)       # 0-10
    score_skills: Mapped[Optional[float]] = mapped_column(Float, nullable=True)            # 0-30
    score_experience: Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # 0-30
    score_seniority: Mapped[Optional[float]] = mapped_column(Float, nullable=True)         # 0-10
    score_authorization: Mapped[Optional[float]] = mapped_column(Float, nullable=True)     # 0-10
    score_company: Mapped[Optional[float]] = mapped_column(Float, nullable=True)           # 0-10

    # Byproducts extracted during matcher (reused for cover letter generation, saves LLM calls)
    match_keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)             # Newline-separated
    match_fit_bullets: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # Newline-separated
    match_connector: Mapped[Optional[str]] = mapped_column(Text, nullable=True)            # One-line hook

    # Status
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.NEW.value)

    # Generated artifact paths
    tailored_resume_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    tailored_resume_pdf_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    cover_letter_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Notes
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    events: Mapped[list["Event"]] = relationship(
        "Event", back_populates="job", cascade="all, delete-orphan"
    )
    revisions: Mapped[list["ResumeRevision"]] = relationship(
        "ResumeRevision", back_populates="job", cascade="all, delete-orphan",
        order_by="ResumeRevision.version_num",
    )


class ResumeRevision(Base):
    """Resume version history: save one version after each conversation modification."""

    __tablename__ = "resume_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    version_num: Mapped[int] = mapped_column(Integer)          # 1, 2, 3 ...
    md_content: Mapped[str] = mapped_column(Text)              # Complete markdown body
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # User or AI change notes
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    job: Mapped["Job"] = relationship("Job", back_populates="revisions")


class Event(Base):
    """Events during application process (interview notifications, HR replies, etc.)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    kind: Mapped[str] = mapped_column(String(32))   # applied / replied / scheduled / rejected ...
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    job: Mapped["Job"] = relationship("Job", back_populates="events")


def get_engine(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", future=True)


def init_db(db_path: Path) -> None:
    """Create tables and run incremental migrations on existing DB (add new columns)."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    # Incremental migration: safely add new columns (SQLite doesn't support ALTER TABLE ADD COLUMN IF NOT EXISTS)
    _migrate_add_columns(engine)


def _migrate_add_columns(engine) -> None:
    """Detect and add columns/tables from new version, skip if existing.
    Use engine.begin() to auto-commit each DDL statement, avoiding cross-connection visibility issues.
    """
    import sqlalchemy as _sa
    migrations = [
        ("jobs", "content_hash", "VARCHAR(16)"),
        ("profiles", "job_types_json", "TEXT"),
        ("jobs", "job_type", "VARCHAR(32)"),
    ]
    # New table migration — use begin() to auto-commit DDL
    with engine.begin() as conn:
        tables = [row[0] for row in conn.execute(
            _sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        )]
        if "resume_revisions" not in tables:
            conn.execute(_sa.text("""
                CREATE TABLE resume_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id),
                    version_num INTEGER NOT NULL,
                    md_content TEXT NOT NULL,
                    note TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            print("[db] migration: created resume_revisions table")

    # Column migration — use independent transaction per column, prevent one failure affecting others
    for table, col, col_type in migrations:
        try:
            with engine.begin() as conn:
                existing = [
                    row[1] for row in
                    conn.execute(_sa.text(f"PRAGMA table_info({table})"))
                ]
                if col not in existing:
                    conn.execute(_sa.text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                    ))
                    # engine.begin() auto-commits, no manual commit needed
                    print(f"[db] migration: added {table}.{col}")
        except Exception as e:
            print(f"[db] migration warning ({table}.{col}): {e}")


def session_scope(db_path: Path) -> Session:
    """Return a Session, caller responsible for commit/close."""
    return Session(get_engine(db_path), future=True)
