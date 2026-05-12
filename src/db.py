"""SQLite 数据库: 岗位表 + 投递追踪."""
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
    NEW = "new"                       # 新抓取,未评分
    SCORED = "scored"                 # 已评分
    SHORTLISTED = "shortlisted"       # 入选,准备投
    APPLIED = "applied"               # 已投递,等待回复
    PHONE_SCREEN = "phone_screen"     # 电话/初筛面试
    HR_INTERVIEW = "hr_interview"     # HR 面试
    HM_INTERVIEW = "hm_interview"     # Hiring Manager 面试
    FINAL_ROUND = "final_round"       # 终面 / onsite
    OFFER = "offer"                   # 收到 Offer
    REJECTED = "rejected"             # 被拒
    ARCHIVED = "archived"             # 自动或手动归档
    # 兼容旧数据
    INTERVIEW = "interview"           # 旧状态,统一归入 hr_interview 展示


class Profile(Base):
    """画像快照: 一次 onboarding 提交对应一行. 历史可回滚."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    label: Mapped[str] = mapped_column(String(120))                # 自动从描述里取前 60 字
    user_description: Mapped[str] = mapped_column(Text)            # 用户原文
    resume_filename: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    profile_json: Mapped[str] = mapped_column(Text)                # ProfileAnalysis 序列化
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)  # 当前活跃画像
    # 每画像独立的自动化设置
    schedule_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)          # 每 N 小时跑一次, 0=关闭
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
    # 跨平台语义去重: sha256(normalize(company)|normalize(title)|normalize(location))[:16]
    content_hash: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    salary: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 岗位实际发布时间(各平台 actor 提供,如有)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 从 JD 中抽取的额外结构化字段 (matcher 阶段填充)
    work_mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)   # remote / hybrid / onsite / unspecified
    min_education: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # high_school / bachelor / master / phd / any / unspecified

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
    tailored_resume_pdf_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
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
    revisions: Mapped[list["ResumeRevision"]] = relationship(
        "ResumeRevision", back_populates="job", cascade="all, delete-orphan",
        order_by="ResumeRevision.version_num",
    )


class ResumeRevision(Base):
    """简历版本历史: 每次对话修改后保存一个版本."""

    __tablename__ = "resume_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    version_num: Mapped[int] = mapped_column(Integer)          # 1, 2, 3 ...
    md_content: Mapped[str] = mapped_column(Text)              # 完整 markdown 正文
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 用户或 AI 的改动说明
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    job: Mapped["Job"] = relationship("Job", back_populates="revisions")


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
    """建表, 并对已有 DB 执行增量 migration (添加新列)."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    # 增量 migration: 安全地添加新列 (SQLite 不支持 ALTER TABLE ADD COLUMN IF NOT EXISTS)
    _migrate_add_columns(engine)


def _migrate_add_columns(engine) -> None:
    """检测并添加新版本引入的列/表, 已存在则跳过.
    使用 engine.begin() 确保每个 DDL 语句自动提交, 避免跨连接可见性问题.
    """
    import sqlalchemy as _sa
    migrations = [
        ("jobs", "content_hash", "VARCHAR(16)"),
        ("profiles", "job_types_json", "TEXT"),
    ]
    # 新表 migration — 用 begin() 自动提交 DDL
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

    # 列 migration — 每列用独立事务，避免一列失败影响其他列
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
                    # engine.begin() 自动 commit，不需要手动调用
                    print(f"[db] migration: added {table}.{col}")
        except Exception as e:
            print(f"[db] migration warning ({table}.{col}): {e}")


def session_scope(db_path: Path) -> Session:
    """返回一个 Session,调用方负责 commit/close."""
    return Session(get_engine(db_path), future=True)
