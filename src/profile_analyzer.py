"""简历画像分析器: 让 DeepSeek 综合"市场热度 × 竞争强度 × 候选人优势"
为候选人找出 Top-10 最优投递岗位.

两段式策略:
1. **第一步: 确定 Top-10 primary** — DeepSeek 必须产出**恰好 10 个**主 title (高精度).
2. **第二步: 模糊扩展** — 每个 primary 自带 2-5 个 aliases + 0-3 个 broader_terms,
   collect 阶段把这些一并扔给搜索引擎, 撒大网兜更多潜在命中.

设计原则:
1. **三维评分**: market_demand / competition (越低越好) / user_advantage
   composite = market_demand * (10 - competition) * user_advantage / 10  范围 0-100.
2. **方向限制**: engineering / research-engineering / academic-research / academic-teaching.
3. **Title 强制为市场真实高频称谓**, 不允许造词.
4. **Schema 严格**, tool_use 强制结构化输出, code 可靠解析.

输出存到 data/resume/_profile.json. 后续 collect 自动读取 top_10_positions
的 title + aliases (+ broader_terms) 作为搜索关键词.
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
                                "academic-research",
                                "academic-teaching",
                            ],
                            "description": "engineering | research-engineering | academic-research (postdoc/RS) | academic-teaching (Asst Prof/TTAP)",
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
                            "minItems": 2,
                            "maxItems": 5,
                            "description": "2-5 条 bullets,引用简历具体项目/数字 (中文)",
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


def analyze_profile(config: Config, user_description: str | None = None) -> ProfileAnalysis:
    """读取简历 + 用户自描述求职需求, 调 DeepSeek 做 Top-10 三维度评分分析.

    user_description 是用户自由输入的"我想找什么样的工作"自然语言,
    优先级高于 config.preferences. 如果传 None 则尝试从磁盘加载.
    """
    resume_text = load_cached(config.path("resume_dir"))
    if user_description is None:
        user_description = load_user_description(config)

    prompt = render(
        load_prompt("profile_analyzer"),
        resume=resume_text,
        user_description=user_description or "(候选人未提供自述,只根据简历和默认偏好推断)",
        preferences=json.dumps(config.preferences, ensure_ascii=False, indent=2),
    )

    role = "profile_analyzer"
    if not config.raw.get("model", {}).get(role):
        role = "matcher"
    client, model_name = make_client(config, role)

    resp = client.messages.create(
        model=model_name,
        max_tokens=4096,
        tools=[PROFILE_TOOL],
        tool_choice={"type": "tool", "name": "submit_profile_analysis"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_profile_analysis":
            return ProfileAnalysis.from_tool_input(block.input)
    raise RuntimeError("模型没返回 submit_profile_analysis 工具调用")
