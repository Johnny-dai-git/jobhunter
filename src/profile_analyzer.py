"""简历画像分析器: 让 DeepSeek 看简历,推断 Top-5 能力方向 + 对应搜索 title.

工作流:
    parse-resume -> analyze-profile -> [缓存到 data/resume/_profile.json] -> collect 自动用

不写 cache (或者用 --force) 就重新分析. 缓存里包含 5 个方向 + 搜索 title + 地点建议.
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
    "description": "提交对候选人简历的能力方向分析结果",
    "input_schema": {
        "type": "object",
        "properties": {
            "top_directions": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "能力方向名称 (中文)",
                        },
                        "why_match": {
                            "type": "string",
                            "description": "为什么候选人 fit 这个方向, 引用简历具体项目/数字 (中文,<= 60 字)",
                        },
                        "search_titles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 3,
                            "description": "对应的 LinkedIn/Indeed 高频搜索 title (英文,1-3 个,必须是市场上真有的)",
                        },
                    },
                    "required": ["name", "why_match", "search_titles"],
                },
                "description": "5 个最匹配能力方向,最强匹配在前",
            },
            "target_locations": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 5,
                "description": "推荐的目标地点 (LinkedIn 能识别的写法)",
            },
            "summary": {
                "type": "string",
                "description": "候选人核心定位总结 (中文,<= 80 字)",
            },
        },
        "required": ["top_directions", "target_locations", "summary"],
    },
}


@dataclass
class Direction:
    name: str
    why_match: str
    search_titles: list[str]

    @classmethod
    def from_dict(cls, d: dict) -> "Direction":
        return cls(
            name=str(d.get("name", "")),
            why_match=str(d.get("why_match", "")),
            search_titles=list(d.get("search_titles") or []),
        )


@dataclass
class ProfileAnalysis:
    top_directions: list[Direction]
    target_locations: list[str]
    summary: str

    @classmethod
    def from_tool_input(cls, data: dict) -> "ProfileAnalysis":
        return cls(
            top_directions=[Direction.from_dict(d) for d in (data.get("top_directions") or [])],
            target_locations=list(data.get("target_locations") or []),
            summary=str(data.get("summary", "")),
        )

    def to_dict(self) -> dict:
        return {
            "top_directions": [asdict(d) for d in self.top_directions],
            "target_locations": self.target_locations,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileAnalysis":
        return cls(
            top_directions=[Direction(**d) for d in data.get("top_directions", [])],
            target_locations=data.get("target_locations", []),
            summary=data.get("summary", ""),
        )

    def unique_search_titles(self, limit: int = 5) -> list[str]:
        """把 5 个 direction 里的 search_titles 拍平 + 去重 + 取前 N. 保留顺序."""
        seen: set[str] = set()
        out: list[str] = []
        for d in self.top_directions:
            for t in d.search_titles:
                t_low = t.strip().lower()
                if t_low and t_low not in seen:
                    seen.add(t_low)
                    out.append(t.strip())
                if len(out) >= limit:
                    return out
        return out


def _profile_path(config: Config) -> Path:
    return config.path("resume_dir") / "_profile.json"


def save_profile(config: Config, profile: ProfileAnalysis) -> Path:
    path = _profile_path(config)
    path.write_text(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_profile(config: Config) -> ProfileAnalysis | None:
    path = _profile_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ProfileAnalysis.from_dict(data)
    except Exception:
        return None


def analyze_profile(config: Config) -> ProfileAnalysis:
    """读取简历,调 DeepSeek 做 Top-5 能力方向分析,返回结果."""
    resume_text = load_cached(config.path("resume_dir"))
    prompt = render(
        load_prompt("profile_analyzer"),
        resume=resume_text,
        preferences=json.dumps(config.preferences, ensure_ascii=False, indent=2),
    )

    # 用一个独立 role,允许配置不同模型 (默认与 matcher 一致)
    role = "profile_analyzer"
    # 如果 config 没显式配 profile_analyzer 角色,回退到 matcher
    if not config.raw.get("model", {}).get(role):
        role = "matcher"
    client, model_name = make_client(config, role)

    resp = client.messages.create(
        model=model_name,
        max_tokens=3000,
        tools=[PROFILE_TOOL],
        tool_choice={"type": "tool", "name": "submit_profile_analysis"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_profile_analysis":
            return ProfileAnalysis.from_tool_input(block.input)
    raise RuntimeError("模型没返回 submit_profile_analysis 工具调用")
