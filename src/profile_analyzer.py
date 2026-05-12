"""简历画像分析器 (三轮 pipeline 版).

三轮多轮对话策略:
  Round 1 — 提取 (profile_extract.md):
      resume + materials → 结构化技能档案 + ATS 关键词池
  Round 2 — 评估 (profile_perspectives.md):
      提取结果 → HR视角 / HM视角 / 策略师视角 → 候选人定位分析
  Round 3 — 输出 (profile_analyzer.md):
      全部上下文 + 用户需求 → Top-10 positions (含 aliases/broader_terms)

每轮的输出作为下一轮 messages 历史的一部分传入,
让模型在完整上下文中做决策,而不是每次从头开始.

最终输出存到 data/resume/_profile.json,
后续 collect 自动读取 top_10_positions 的 title + aliases + broader_terms 作为搜索关键词.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from .agent import load_prompt, make_client, render
from .config import Config
from .db import Profile, session_scope
from .resume_reader import load_cached


_COMPANY_LIST_SCHEMA = {
    "type": "array",
    "minItems": 3,
    "maxItems": 8,
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "why_fit": {"type": "string", "description": "为什么对候选人 fit (中文,<=50字)"},
            "hiring_signal": {"type": "string", "description": "当前扩张/招聘信号 (中文,<=50字)"},
            "example_roles": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
                "description": "可能的具体岗位 title 例子 (英文)",
            },
            "careers_url": {"type": "string", "description": "招聘页 URL (可选)"},
        },
        "required": ["name", "why_fit", "hiring_signal", "example_roles"],
    },
}


PROFILE_TOOL: dict[str, Any] = {
    "name": "submit_profile_analysis",
    "description": "提交对候选人的 Top-10 最优投递岗位分析",
    "input_schema": {
        "type": "object",
        "properties": {
            "top_10_positions": {
                "type": "array",
                "minItems": 10,
                "maxItems": 10,
                "description": "Top 10 投递岗位, 按 composite 降序. 后续会用 aliases + broader_terms 模糊扩展.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "LinkedIn 标准 title (英文,不许造词)",
                        },
                        "direction": {
                            "type": "string",
                            "enum": [
                                "engineering",
                                "research-engineering",
                                "internship",
                            ],
                            "description": (
                                "engineering (SWE/MLE/Backend/Platform/Infra 全职) | "
                                "research-engineering (Anthropic/DeepMind 类工业实验室全职) | "
                                "internship (实习岗位，title 需含 Intern/Co-op). "
                                "仅 industry，不做 academic."
                            ),
                        },
                        "scores": {
                            "type": "object",
                            "properties": {
                                "market_demand": {
                                    "type": "integer", "minimum": 0, "maximum": 10,
                                    "description": "当前市场招聘量",
                                },
                                "competition": {
                                    "type": "integer", "minimum": 0, "maximum": 10,
                                    "description": "竞争强度,越低越好",
                                },
                                "user_advantage": {
                                    "type": "integer", "minimum": 0, "maximum": 10,
                                    "description": "候选人匹配深度",
                                },
                                "composite": {
                                    "type": "integer", "minimum": 0, "maximum": 100,
                                    "description": "= market_demand * (10-competition) * user_advantage / 10",
                                },
                            },
                            "required": ["market_demand", "competition", "user_advantage", "composite"],
                        },
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 5,
                            "description": "2-5 个市场真实存在的同义/变体 title (英文)",
                        },
                        "broader_terms": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 0,
                            "maxItems": 3,
                            "description": "0-3 个可能隐藏此方向的广义 title (英文,例如某些AI公司用 'Software Engineer' 实际是 ML 岗)",
                        },
                        "why_this_position": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 4,
                            "maxItems": 6,
                            "description": (
                                "4-6 条 bullets (中文), 前 2 条以 '[HR]' 开头: "
                                "HR 视角——关键词覆盖/学历门槛/title 匹配; "
                                "后 2-4 条以 '[HM]' 开头: "
                                "HM 视角——引用简历或资料库里的具体项目/数字/论文/开源贡献, "
                                "说明候选人能给团队带来的差异化价值"
                            ),
                        },
                        "market_evidence": {
                            "type": "string",
                            "description": "市场为什么旺(<=50字,中文)",
                        },
                        "linkedin_search_url": {
                            "type": "string",
                            "description": "直接可点开的 LinkedIn 搜索 URL",
                        },
                    },
                    "required": [
                        "title", "direction", "scores", "aliases", "broader_terms",
                        "why_this_position", "market_evidence", "linkedin_search_url",
                    ],
                },
            },
            "target_locations": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 5,
                "description": "推荐目标地点 (LinkedIn 可识别)",
            },
            "recommended_companies": {
                "type": "object",
                "description": "按 5 个区域分组的目标公司清单 (背景匹配+积极扩张)",
                "properties": {
                    "north_america": _COMPANY_LIST_SCHEMA,
                    "hong_kong": _COMPANY_LIST_SCHEMA,
                    "singapore": _COMPANY_LIST_SCHEMA,
                    "japan": _COMPANY_LIST_SCHEMA,
                    "europe": _COMPANY_LIST_SCHEMA,
                },
                "required": ["north_america", "hong_kong", "singapore", "japan", "europe"],
            },
            "summary": {
                "type": "string",
                "description": "候选人核心定位+投递策略(<=80字,中文)",
            },
        },
        "required": ["top_10_positions", "target_locations", "recommended_companies", "summary"],
    },
}


# ── Round 1 tool: 技能提取 ──────────────────────────────────────────────────

EXTRACT_TOOL: dict[str, Any] = {
    "name": "submit_skill_extraction",
    "description": "提交从简历和资料库中提取的结构化技能档案",
    "input_schema": {
        "type": "object",
        "properties": {
            "technical_skills": {
                "type": "array",
                "description": "所有技术技能列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "skill":    {"type": "string"},
                        "level":    {"type": "string",
                                     "enum": ["exposure", "proficient", "deep_project", "published_or_open_source"],
                                     "description": "接触过 / 熟练 / 深度项目经验 / 发表或开源贡献"},
                        "source":   {"type": "string",
                                     "enum": ["resume", "materials", "both"]},
                        "evidence": {"type": "string",
                                     "description": "一句话说明证据, 例如: '用 PyTorch 实现 LLM 推理引擎, 吞吐量提升 3x'"},
                    },
                    "required": ["skill", "level", "source"],
                },
            },
            "key_projects": {
                "type": "array",
                "description": "最重要的 3-6 个项目/经历",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":       {"type": "string"},
                        "scale":      {"type": "string", "description": "独立/小团队/大型系统"},
                        "impact":     {"type": "string", "description": "量化影响, 尽量有数字"},
                        "tech_stack": {"type": "array", "items": {"type": "string"}},
                        "source":     {"type": "string", "enum": ["resume", "materials", "both"]},
                    },
                    "required": ["name", "impact", "tech_stack"],
                },
            },
            "materials_highlights": {
                "type": "array",
                "description": "资料库中展示的、简历里未充分体现的深度内容 (论文/文章/开源等)",
                "items": {
                    "type": "object",
                    "properties": {
                        "title":       {"type": "string"},
                        "type":        {"type": "string",
                                        "enum": ["paper", "article", "open_source", "project_doc", "other"]},
                        "tech_depth":  {"type": "string", "description": "技术方向和深度一句话描述"},
                        "signal":      {"type": "string", "description": "外部认可信号: 发表期刊/GitHub stars/引用数等"},
                    },
                    "required": ["title", "type", "tech_depth"],
                },
            },
            "ats_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "所有材料中提炼的 ATS 关键词池, 20-50 个, 供 HR 视角评估和 JD 匹配",
            },
            "experience_years": {
                "type": "number",
                "description": "总工作年限 (不含在读)",
            },
            "education_level": {
                "type": "string",
                "enum": ["bachelor", "master", "phd", "other"],
            },
        },
        "required": ["technical_skills", "key_projects", "ats_keywords",
                     "experience_years", "education_level"],
    },
}


# ── Round 2 tool: 三视角评估 ────────────────────────────────────────────────

PERSPECTIVES_TOOL: dict[str, Any] = {
    "name": "submit_perspectives",
    "description": "提交 HR / HM / 策略师三视角对候选人的综合评估",
    "input_schema": {
        "type": "object",
        "properties": {
            "hr_view": {
                "type": "object",
                "properties": {
                    "strong_match_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "HR 会直接放行的方向 (关键词强覆盖 + 门槛满足)",
                    },
                    "conditional_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "HR 会犹豫的方向 (部分满足, 需要补充材料或降一级投)",
                    },
                    "weak_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "HR 会直接 pass 的方向",
                    },
                    "keyword_coverage_note": {
                        "type": "string",
                        "description": "ATS 关键词覆盖情况的简短说明",
                    },
                },
                "required": ["strong_match_directions", "conditional_directions", "keyword_coverage_note"],
            },
            "hm_view": {
                "type": "object",
                "properties": {
                    "proven_delivery": {
                        "type": "array", "items": {"type": "string"},
                        "description": "有明确交付证明的技术方向 (引用具体项目/数字/论文)",
                    },
                    "differentiators": {
                        "type": "array", "items": {"type": "string"},
                        "description": "HM 会认为候选人有差异化价值的特质 (大多数同级别竞争者没有的)",
                    },
                    "main_concerns": {
                        "type": "array", "items": {"type": "string"},
                        "description": "HM 最可能的疑虑 (例如: 某方向年限短/广度有余深度不足)",
                    },
                    "high_value_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "HM 视角下候选人能带来独特价值的方向",
                    },
                },
                "required": ["proven_delivery", "differentiators", "high_value_directions"],
            },
            "strategist_view": {
                "type": "object",
                "properties": {
                    "high_roi_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "需求旺 + 候选人有竞争优势的高性价比方向",
                    },
                    "avoid_directions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "竞争太激烈或候选人优势不明显、建议避开的方向",
                    },
                    "key_positioning": {
                        "type": "string",
                        "description": "一句话总结候选人最应该主打的市场定位 (中文, <=50字)",
                    },
                },
                "required": ["high_roi_directions", "key_positioning"],
            },
            "combined_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "综合三视角后, 在简历/cover letter 中应重点强调的关键词, 15-30 个, 按重要性降序",
            },
        },
        "required": ["hr_view", "hm_view", "strategist_view", "combined_keywords"],
    },
}


@dataclass
class PositionScores:
    market_demand: int = 0
    competition: int = 0
    user_advantage: int = 0
    composite: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "PositionScores":
        return cls(
            market_demand=int(d.get("market_demand", 0)),
            competition=int(d.get("competition", 0)),
            user_advantage=int(d.get("user_advantage", 0)),
            composite=int(d.get("composite", 0)),
        )


@dataclass
class Position:
    title: str
    direction: str
    scores: PositionScores
    aliases: list[str] = field(default_factory=list)
    broader_terms: list[str] = field(default_factory=list)
    why_this_position: list[str] = field(default_factory=list)
    market_evidence: str = ""
    linkedin_search_url: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            title=str(d.get("title", "")).strip(),
            direction=str(d.get("direction", "engineering")),
            scores=PositionScores.from_dict(d.get("scores") or {}),
            aliases=list(d.get("aliases") or []),
            broader_terms=list(d.get("broader_terms") or []),
            why_this_position=list(d.get("why_this_position") or []),
            market_evidence=str(d.get("market_evidence", "")),
            linkedin_search_url=str(d.get("linkedin_search_url", "")),
        )

    def all_search_terms(self, include_broader: bool = False) -> list[str]:
        """primary + aliases [+ broader_terms].  保持顺序,去除空串."""
        out = [self.title] + list(self.aliases)
        if include_broader:
            out += list(self.broader_terms)
        return [t.strip() for t in out if t and t.strip()]


@dataclass
class Company:
    name: str
    why_fit: str = ""
    hiring_signal: str = ""
    example_roles: list[str] = field(default_factory=list)
    careers_url: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Company":
        return cls(
            name=str(d.get("name", "")).strip(),
            why_fit=str(d.get("why_fit", "")),
            hiring_signal=str(d.get("hiring_signal", "")),
            example_roles=list(d.get("example_roles") or []),
            careers_url=str(d.get("careers_url", "")),
        )


@dataclass
class RegionalCompanies:
    north_america: list[Company] = field(default_factory=list)
    hong_kong: list[Company] = field(default_factory=list)
    singapore: list[Company] = field(default_factory=list)
    japan: list[Company] = field(default_factory=list)
    europe: list[Company] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "RegionalCompanies":
        if not d:
            return cls()
        return cls(
            north_america=[Company.from_dict(c) for c in (d.get("north_america") or [])],
            hong_kong=[Company.from_dict(c) for c in (d.get("hong_kong") or [])],
            singapore=[Company.from_dict(c) for c in (d.get("singapore") or [])],
            japan=[Company.from_dict(c) for c in (d.get("japan") or [])],
            europe=[Company.from_dict(c) for c in (d.get("europe") or [])],
        )

    def to_dict(self) -> dict:
        return {
            "north_america": [asdict(c) for c in self.north_america],
            "hong_kong": [asdict(c) for c in self.hong_kong],
            "singapore": [asdict(c) for c in self.singapore],
            "japan": [asdict(c) for c in self.japan],
            "europe": [asdict(c) for c in self.europe],
        }

    def regions(self) -> list[tuple[str, list[Company]]]:
        """按显示顺序返回 (region_label, companies)."""
        return [
            ("北美 (US/Canada)", self.north_america),
            ("香港", self.hong_kong),
            ("新加坡", self.singapore),
            ("日本", self.japan),
            ("欧洲", self.europe),
        ]


@dataclass
class ProfileAnalysis:
    top_10_positions: list[Position]
    target_locations: list[str]
    summary: str
    recommended_companies: RegionalCompanies = field(default_factory=RegionalCompanies)

    @classmethod
    def from_tool_input(cls, data: dict) -> "ProfileAnalysis":
        # 新 schema 是 top_10_positions; 兼容老 schema 还在用 top_5_positions
        raw = data.get("top_10_positions") or data.get("top_5_positions") or []
        positions = [Position.from_dict(p) for p in raw]
        positions.sort(key=lambda p: -p.scores.composite)
        return cls(
            top_10_positions=positions,
            target_locations=list(data.get("target_locations") or []),
            summary=str(data.get("summary", "")),
            recommended_companies=RegionalCompanies.from_dict(data.get("recommended_companies") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "top_10_positions": [
                {
                    "title": p.title,
                    "direction": p.direction,
                    "scores": asdict(p.scores),
                    "aliases": p.aliases,
                    "broader_terms": p.broader_terms,
                    "why_this_position": p.why_this_position,
                    "market_evidence": p.market_evidence,
                    "linkedin_search_url": p.linkedin_search_url,
                }
                for p in self.top_10_positions
            ],
            "target_locations": self.target_locations,
            "summary": self.summary,
            "recommended_companies": self.recommended_companies.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileAnalysis":
        # 兼容老 JSON 文件 (top_5_positions) 和新 (top_10_positions)
        raw = data.get("top_10_positions") or data.get("top_5_positions") or []
        positions = [Position.from_dict(p) for p in raw]
        positions.sort(key=lambda p: -p.scores.composite)
        return cls(
            top_10_positions=positions,
            target_locations=data.get("target_locations", []),
            summary=data.get("summary", ""),
            recommended_companies=RegionalCompanies.from_dict(data.get("recommended_companies") or {}),
        )

    def search_titles(
        self, *, include_aliases: bool = True, include_broader: bool = True, limit: int = 40
    ) -> list[str]:
        """聚合 10 个 position 的搜索词. 模糊扩展默认全开 — primary + aliases + broader.

        流程: 先 10 个 primary 兜底, 再展开 aliases (同义词), 再展开 broader_terms
        (隐藏机会). 去重 case-insensitive, 保持顺序.
        """
        seen: set[str] = set()
        out: list[str] = []
        # 先把所有 primary 收齐 (确保 Top-10 一个不漏)
        for p in self.top_10_positions:
            t = (p.title or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        # 再模糊扩展: aliases 然后 broader_terms
        for tier in ("aliases", "broader"):
            for p in self.top_10_positions:
                terms: list[str] = []
                if tier == "aliases" and include_aliases:
                    terms = list(p.aliases)
                elif tier == "broader" and include_broader:
                    terms = list(p.broader_terms)
                for t in terms:
                    t_clean = (t or "").strip()
                    if t_clean and t_clean.lower() not in seen:
                        seen.add(t_clean.lower())
                        out.append(t_clean)
                    if len(out) >= limit:
                        return out
        return out

    def primary_titles(self) -> list[str]:
        """只取 10 个 primary title."""
        return [p.title for p in self.top_10_positions if p.title]

    # 向后兼容: 旧代码用过 unique_search_titles / top_5_positions
    def unique_search_titles(self, limit: int = 40) -> list[str]:
        return self.search_titles(include_aliases=True, include_broader=True, limit=limit)

    @property
    def top_5_positions(self) -> list[Position]:
        """老接口兼容: 返回前 5 个 (按 composite 已排序)."""
        return self.top_10_positions[:5]


def _profile_path(config: Config) -> Path:
    return config.path("resume_dir") / "_profile.json"


def _user_desc_path(config: Config) -> Path:
    return config.path("resume_dir") / "_user_description.txt"


def save_user_description(config: Config, text: str) -> Path:
    path = _user_desc_path(config)
    path.write_text(text.strip(), encoding="utf-8")
    return path


def load_user_description(config: Config) -> str | None:
    path = _user_desc_path(config)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def save_profile(config: Config, profile: ProfileAnalysis) -> Path:
    """保存 profile 到 JSON 文件 (用于代码读取)."""
    path = _profile_path(config)
    path.write_text(
        json.dumps(profile.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def save_profile_snapshot(
    config: Config,
    profile: ProfileAnalysis,
    user_description: str,
    resume_filename: str | None = None,
) -> int:
    """落 profile 到 DB (profiles 表) 一条新行,标为 current. 返回 profile_id.

    跟 save_profile 配合: JSON 是"当前激活的画像", DB 是历史快照.
    """
    label = (user_description.strip().splitlines()[0] if user_description else "")[:60] or "Profile"
    profile_json = json.dumps(profile.to_dict(), ensure_ascii=False)
    db_path = config.path("db_path")

    with session_scope(db_path) as session:
        # 旧的 current 取消激活
        session.execute(update(Profile).where(Profile.is_current).values(is_current=False))
        new_row = Profile(
            label=label,
            user_description=user_description,
            resume_filename=resume_filename,
            profile_json=profile_json,
            is_current=True,
        )
        session.add(new_row)
        session.commit()
        return new_row.id


def list_profile_snapshots(config: Config) -> list[Profile]:
    """返回所有历史画像快照,新 → 老."""
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        rows = list(session.scalars(
            select(Profile).order_by(Profile.created_at.desc())
        ).all())
        for r in rows:
            session.expunge(r)
        return rows


def get_current_profile_id(config: Config) -> int | None:
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        row = session.scalar(select(Profile).where(Profile.is_current))
        return row.id if row else None


def activate_profile_snapshot(config: Config, profile_id: int) -> Profile:
    """把某历史画像设为当前,同时把它的 profile_json 写回 JSON 文件 + 同步 user_description.

    返回激活的 Profile 对象.
    """
    db_path = config.path("db_path")
    with session_scope(db_path) as session:
        row = session.get(Profile, profile_id)
        if not row:
            raise ValueError(f"Profile #{profile_id} 不存在")

        session.execute(update(Profile).where(Profile.is_current).values(is_current=False))
        row.is_current = True
        session.add(row)
        session.commit()

        # 同步 JSON + 用户描述文件
        _profile_path(config).write_text(row.profile_json, encoding="utf-8")
        save_user_description(config, row.user_description)
        session.expunge(row)
        return row


def load_profile(config: Config) -> ProfileAnalysis | None:
    path = _profile_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 兼容旧版本格式 (top_directions): 如果是旧版,返回 None 强制重新生成
        if (
            "top_directions" in data
            and "top_5_positions" not in data
            and "top_10_positions" not in data
        ):
            print("[profile] 旧版 _profile.json 不兼容,请跑 `analyze-profile --force` 重新生成")
            return None
        return ProfileAnalysis.from_dict(data)
    except Exception as e:
        print(f"[profile] 加载缓存失败: {e}")
        return None


def analyze_profile(
    config: Config,
    user_description: str | None = None,
    job_types: list[str] | None = None,
) -> ProfileAnalysis:
    """三轮 pipeline 分析候选人画像.

    Round 1: resume + materials → 结构化技能提取
    Round 2: 提取结果 → HR / HM / 策略师三视角评估
    Round 3: 全部上下文 + 用户需求 + job_types → Top-10 positions

    job_types: ["Full-time"] / ["Internship"] / ["Full-time","Internship"]
    不同类型会生成不同定位的 Top-10（实习 vs 正式员工）.
    """
    from .resume_reader import read_materials

    resume_text = load_cached(config.path("resume_dir"))
    if user_description is None:
        user_description = load_user_description(config)
    materials_text = read_materials(config.path("materials_dir"))
    materials_str = materials_text or "(候选人未上传任何资料库材料)"

    # 构建 job_types 上下文说明，注入到 Round 3
    if not job_types:
        job_types = config.preferences.get("job_types") or ["Full-time"]
    is_intern_only  = job_types == ["Internship"] or job_types == ["intern"]
    is_ft_only      = "Internship" not in job_types and "intern" not in [j.lower() for j in job_types]
    if is_intern_only:
        job_type_instruction = (
            "**本次分析专门针对实习岗位（Internship）。**\n"
            "Top-10 方向必须全部是实习职位，title 中明确包含 'Intern'、'Internship' 或 'Co-op'。\n"
            "评估标准以在校生或应届生视角为主，不要求多年工作经验。\n"
            "搜索词（aliases/broader_terms）也应包含实习相关变体，如 'Software Engineer Intern'、'ML Research Intern' 等。"
        )
    elif is_ft_only:
        job_type_instruction = (
            "**本次分析专门针对正式全职岗位（Full-time）。**\n"
            "Top-10 方向全部面向全职职位，不要包含任何实习（Intern/Internship/Co-op）title。\n"
            "aliases 和 broader_terms 中也不得出现实习相关词汇。"
        )
    else:
        # 多个类型时，profile 分析生成通用 Top-10，collect 阶段分别跑每个类型
        job_type_instruction = (
            f"**本次分析覆盖：{', '.join(job_types)}（采集阶段将分别独立运行两次）。**\n"
            "Top-10 请生成通用岗位方向，不带 Intern 后缀，collect 阶段会分别用 Full-time 和 Internship 过滤器各跑一次。\n"
            "aliases 和 broader_terms 同样使用通用写法。"
        )

    role = "profile_analyzer"
    if not config.raw.get("model", {}).get(role):
        role = "matcher"
    client, model_name = make_client(config, role)

    # ── Round 1: 技能提取 ───────────────────────────────────────────────────
    print("[profile] Round 1 — 提取技能档案和关键词...")
    r1_prompt = render(
        load_prompt("profile_extract"),
        resume=resume_text,
        materials=materials_str,
    )
    r1_resp = client.messages.create(
        model=model_name,
        max_tokens=3000,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "submit_skill_extraction"},
        messages=[{"role": "user", "content": r1_prompt}],
    )
    r1_tool_result = None
    for block in r1_resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_skill_extraction":
            r1_tool_result = block.input
            break
    if r1_tool_result is None:
        raise RuntimeError("[profile] Round 1 失败: 模型未返回 submit_skill_extraction")

    # 提取 tool_use_id，供 tool_result 引用（没有 default，失败就 raise）
    _r1_tool_id = next(
        (b.id for b in r1_resp.content if getattr(b, "type", None) == "tool_use"), None
    )
    if not _r1_tool_id:
        raise RuntimeError("[profile] Round 1 response missing tool_use block id")

    r1_summary = json.dumps(r1_tool_result, ensure_ascii=False, indent=2)
    print(f"[profile] Round 1 完成: 提取 {len(r1_tool_result.get('technical_skills', []))} 项技能, "
          f"{len(r1_tool_result.get('ats_keywords', []))} 个关键词")

    # ── Round 2: 三视角评估 (携带 Round 1 的完整对话历史) ───────────────────
    print("[profile] Round 2 — HR / HM / 策略师三视角评估...")
    r2_prompt = load_prompt("profile_perspectives")
    messages: list[dict] = [
        {"role": "user", "content": r1_prompt},
        # Round 1 的 assistant 回复 (工具调用 + 结果) 作为历史
        {"role": "assistant", "content": r1_resp.content},
        {"role": "user", "content": [
            {
                "type": "tool_result",
                "tool_use_id": _r1_tool_id,
                "content": r1_summary,
            }
        ]},
        {"role": "assistant", "content": "已完成技能提取。开始三视角分析。"},
        {"role": "user", "content": r2_prompt},
    ]
    r2_resp = client.messages.create(
        model=model_name,
        max_tokens=3000,
        tools=[PERSPECTIVES_TOOL],
        tool_choice={"type": "tool", "name": "submit_perspectives"},
        messages=messages,
    )
    r2_tool_result = None
    for block in r2_resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_perspectives":
            r2_tool_result = block.input
            break
    if r2_tool_result is None:
        raise RuntimeError("[profile] Round 2 失败: 模型未返回 submit_perspectives")

    _r2_tool_id = next(
        (b.id for b in r2_resp.content if getattr(b, "type", None) == "tool_use"), None
    )
    if not _r2_tool_id:
        raise RuntimeError("[profile] Round 2 response missing tool_use block id")

    r2_summary = json.dumps(r2_tool_result, ensure_ascii=False, indent=2)
    combined_kw = r2_tool_result.get("combined_keywords", [])
    print(f"[profile] Round 2 完成: {len(combined_kw)} 个核心关键词, "
          f"强匹配方向: {r2_tool_result.get('hr_view', {}).get('strong_match_directions', [])}")

    # ── Round 3: Top-10 输出 (携带全部对话历史) ─────────────────────────────
    print("[profile] Round 3 — 生成 Top-10 最优投递方向...")
    r3_prompt = render(
        load_prompt("profile_analyzer"),
        user_description=user_description or "(候选人未提供自述,只根据简历和默认偏好推断)",
        preferences=json.dumps(config.preferences, ensure_ascii=False, indent=2),
        job_type_instruction=job_type_instruction,
    )
    messages_r3: list[dict] = [
        *messages,
        {"role": "assistant", "content": r2_resp.content},
        {"role": "user", "content": [
            {
                "type": "tool_result",
                "tool_use_id": _r2_tool_id,
                "content": r2_summary,
            }
        ]},
        {"role": "assistant", "content": "已完成三视角评估。开始生成 Top-10 投递方向。"},
        {"role": "user", "content": r3_prompt},
    ]
    r3_resp = client.messages.create(
        model=model_name,
        max_tokens=4096,
        tools=[PROFILE_TOOL],
        tool_choice={"type": "tool", "name": "submit_profile_analysis"},
        messages=messages_r3,
    )
    for block in r3_resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_profile_analysis":
            print("[profile] Round 3 完成: Top-10 生成成功")
            return ProfileAnalysis.from_tool_input(block.input)
    raise RuntimeError("[profile] Round 3 失败: 模型未返回 submit_profile_analysis")
