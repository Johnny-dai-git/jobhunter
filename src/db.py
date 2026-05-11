"""SQLite 数据库: 岗位表 + 投递追踪."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from sqlalchemy import (
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
    NEW = "new"                  # 新抓取,未评分
    SCORED = "scored"            # 已评分
    SHORTLISTED = "shortlisted"  # 入选,准备投
    APPLIED = "applied"          # 已投
    INTERVIEW = "interview"      # 面试中
    OFFER = "offer"
    REJECTED = "rejected"
    ARCHIVED = "archived"        # 自动或手动归档


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))            # linkedin / indeed / manual
    external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(256))
    company: Mapped[str] = mapped_column(String(256))
    location: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    salary: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 评分字段 — 总分
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    match_strengths: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    match_gaps: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 6 维度子评分 (借鉴 DailyJobMatch)
    score_background: Mapped[Optional[float]] = mapped_column(Float, nullable=True)       # 0-10
    score_skills: Mapped[Optional[float]] = mapped_column(Float, nullable=True)            # 0-30
    score_experience: Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # 0-30
    score_seniority: Mapped[Optional[float]] = mapped_column(Float, nullable=True)         # 0-10
    score_authorization: Mapped[Optional[float]] = mapped_column(Float, nullable=True)     # 0-10
    score_company: Mapped[Optional[float]] = mapped_column(Float, nullable=True)           # 0-10

    # matcher 顺手提取的产物 (复用到 cover letter 生成,省 LLM 调用)
    match_keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)             # 换行分隔
    match_fit_bullets: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # 换行分隔
    match_connector: Mapped[Optional[str]] = mapped_column(Text, nullable=True)            # 一句话钩子

    # 状态
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.NEW.value)

    # 生成的产物路径
    tailored_resume_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    cover_letter_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 笔记
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    events: Mapped[list["Event"]] = relationship(
        "Event", back_populates="job", cascade="all, delete-orphan"
    )


class Event(Base):
    """投递过程中的事件 (面试通知、HR 回复等)."""

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
    """建表."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)


def session_scope(db_path: Path) -> Session:
    """返回一个 Session,调用方负责 commit/close."""
    return Session(get_engine(db_path), future=True)
