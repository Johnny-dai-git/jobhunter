"""简历画像分析器: 让 DeepSeek 综合"市场热度 × 竞争强度 × 候选人优势"
为候选人找出 Top-5 最优投递岗位.

设计原则 (跟之前一版的不同):
1. **三维评分**: market_demand / competition (越低越好) / user_advantage
   composite = market_demand * (10 - competition) * user_advantage / 10  范围 0-100.
2. **方向限制**: 仅 engineering / research-engineering, 排除管理/产品/销售岗.
3. **Title 强制为市场真实高频称谓**, 不允许造词.
4. **Schema 严格**, tool_use 强制结构化输出, code 可靠解析.

输出存到 data/resume/_profile.json. 后续 collect 自动读取 top_5_positions
的 title 列表作为搜索关键词.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from .agent import load_prompt, make_client, render
from .config import Config
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
    "description": "提交对候选人的 Top-5 最优投递岗位分析",
    "input_schema": {
        "type": "object",
        "properties": {
            "top_5_positions": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "description": "Top 5 投递岗位, 按 composite 降序",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "LinkedIn 标准 title (英文,不许造词)",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["engineering", "research-engineering"],
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
        "required": ["top_5_positions", "target_locations", "recommended_companies", "summary"],
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
    top_5_positions: list[Position]
    target_locations: list[str]
    summary: str
    recommended_companies: RegionalCompanies = field(default_factory=RegionalCompanies)

    @classmethod
    def from_tool_input(cls, data: dict) -> "ProfileAnalysis":
        positions = [Position.from_dict(p) for p in (data.get("top_5_positions") or [])]
        positions.sort(key=lambda p: -p.scores.composite)
        return cls(
            top_5_positions=positions,
            target_locations=list(data.get("target_locations") or []),
            summary=str(data.get("summary", "")),
            recommended_companies=RegionalCompanies.from_dict(data.get("recommended_companies") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "top_5_positions": [
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
                for p in self.top_5_positions
            ],
            "target_locations": self.target_locations,
            "summary": self.summary,
            "recommended_companies": self.recommended_companies.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileAnalysis":
        positions = [Position.from_dict(p) for p in data.get("top_5_positions", [])]
        positions.sort(key=lambda p: -p.scores.composite)
        return cls(
            top_5_positions=positions,
            target_locations=data.get("target_locations", []),
            summary=data.get("summary", ""),
            recommended_companies=RegionalCompanies.from_dict(data.get("recommended_companies") or {}),
        )

    def search_titles(
        self, *, include_aliases: bool = True, include_broader: bool = False, limit: int = 12
    ) -> list[str]:
        """聚合 5 个 position 的搜索词. 默认包含 aliases (扩大命中面), 不含 broader.

        去重 case-insensitive, 保持顺序 (primary 优先, 然后是 aliases).
        """
        seen: set[str] = set()
        out: list[str] = []
        for p in self.top_5_positions:
            # 先收 primary
            terms = [p.title]
            if include_aliases:
                terms += p.aliases
            if include_broader:
                terms += p.broader_terms
            for t in terms:
                t_clean = (t or "").strip()
                t_low = t_clean.lower()
                if t_clean and t_low not in seen:
                    seen.add(t_low)
                    out.append(t_clean)
                if len(out) >= limit:
                    return out
        return out

    def primary_titles(self) -> list[str]:
        """只取 5 个 primary title (老逻辑兼容)."""
        return [p.title for p in self.top_5_positions if p.title]

    # 向后兼容: 旧代码用过 unique_search_titles
    def unique_search_titles(self, limit: int = 12) -> list[str]:
        return self.search_titles(include_aliases=True, limit=limit)


def _profile_path(config: Config) -> Path:
    return config.path("resume_dir") / "_profile.json"


def save_profile(config: Config, profile: ProfileAnalysis) -> Path:
    path = _profile_path(config)
    path.write_text(
        json.dumps(profile.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_profile(config: Config) -> ProfileAnalysis | None:
    path = _profile_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 兼容旧版本格式 (top_directions): 如果是旧版,返回 None 强制重新生成
        if "top_directions" in data and "top_5_positions" not in data:
            print("[profile] 旧版 _profile.json 不兼容,请跑 `analyze-profile --force` 重新生成")
            return None
        return ProfileAnalysis.from_dict(data)
    except Exception as e:
        print(f"[profile] 加载缓存失败: {e}")
        return None


def analyze_profile(config: Config) -> ProfileAnalysis:
    """读取简历, 调 DeepSeek 做 Top-5 三维度评分分析."""
    resume_text = load_cached(config.path("resume_dir"))
    prompt = render(
        load_prompt("profile_analyzer"),
        resume=resume_text,
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
