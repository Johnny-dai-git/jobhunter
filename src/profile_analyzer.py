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
                        "title", "direction", "scores",
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
            "summary": {
                "type": "string",
                "description": "候选人核心定位+投递策略(<=80字,中文)",
            },
        },
        "required": ["top_5_positions", "target_locations", "summary"],
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
    why_this_position: list[str]
    market_evidence: str
    linkedin_search_url: str

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            title=str(d.get("title", "")).strip(),
            direction=str(d.get("direction", "engineering")),
            scores=PositionScores.from_dict(d.get("scores") or {}),
            why_this_position=list(d.get("why_this_position") or []),
            market_evidence=str(d.get("market_evidence", "")),
            linkedin_search_url=str(d.get("linkedin_search_url", "")),
        )


@dataclass
class ProfileAnalysis:
    top_5_positions: list[Position]
    target_locations: list[str]
    summary: str

    @classmethod
    def from_tool_input(cls, data: dict) -> "ProfileAnalysis":
        positions = [Position.from_dict(p) for p in (data.get("top_5_positions") or [])]
        # 强制按 composite 降序 (模型有时不严格)
        positions.sort(key=lambda p: -p.scores.composite)
        return cls(
            top_5_positions=positions,
            target_locations=list(data.get("target_locations") or []),
            summary=str(data.get("summary", "")),
        )

    def to_dict(self) -> dict:
        return {
            "top_5_positions": [
                {
                    "title": p.title,
                    "direction": p.direction,
                    "scores": asdict(p.scores),
                    "why_this_position": p.why_this_position,
                    "market_evidence": p.market_evidence,
                    "linkedin_search_url": p.linkedin_search_url,
                }
                for p in self.top_5_positions
            ],
            "target_locations": self.target_locations,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileAnalysis":
        positions = [Position.from_dict(p) for p in data.get("top_5_positions", [])]
        positions.sort(key=lambda p: -p.scores.composite)
        return cls(
            top_5_positions=positions,
            target_locations=data.get("target_locations", []),
            summary=data.get("summary", ""),
        )

    def search_titles(self) -> list[str]:
        """5 个 position 的 title list. 已经在 prompt 里要求 distinct."""
        # 去重保险
        seen: set[str] = set()
        out: list[str] = []
        for p in self.top_5_positions:
            t = p.title.strip()
            t_low = t.lower()
            if t and t_low not in seen:
                seen.add(t_low)
                out.append(t)
        return out

    # 向后兼容: 旧代码用过 unique_search_titles
    def unique_search_titles(self, limit: int = 5) -> list[str]:
        return self.search_titles()[:limit]


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
