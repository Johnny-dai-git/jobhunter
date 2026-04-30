"""LLM 调用的薄封装 (Anthropic 兼容接口,Claude / DeepSeek 共用)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from .config import Config


_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def make_client(config: Config, role: str) -> tuple[Anthropic, str]:
    """根据 role 返回 (client, model_name).

    DeepSeek V4 直接兼容 Anthropic SDK,只需换 base_url 和 api_key.
    """
    role_cfg = config.role_config(role)
    provider = role_cfg["provider"]
    model_name = role_cfg["name"]
    api_key = config.api_key_for(provider)
    settings = config.provider_settings(provider)
    base_url = settings.get("base_url") or None  # 空字符串视为 None
    if base_url:
        client = Anthropic(api_key=api_key, base_url=base_url)
    else:
        client = Anthropic(api_key=api_key)
    return client, model_name


def load_prompt(name: str) -> str:
    """从 prompts/<name>.md 加载模板."""
    path = _PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 模板不存在: {path}")
    return path.read_text(encoding="utf-8")


def render(template: str, **kwargs: Any) -> str:
    """轻量级模板替换: {{ name }} -> kwargs['name']. 故意不引入 jinja2 增加心智负担."""
    out = template
    for k, v in kwargs.items():
        out = out.replace(f"{{{{ {k} }}}}", str(v))
        out = out.replace(f"{{{{{k}}}}}", str(v))
    return out


class ClaudeClient:
    """单次文本补全的便捷封装 — 名字保留为 ClaudeClient 是为了向后兼容,
    实际上根据 role 决定走 Claude 还是 DeepSeek."""

    def __init__(self, config: Config):
        self.config = config

    def complete(
        self,
        role: str,
        user_message: str,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        client, model = make_client(self.config, role)
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens or self.config.max_tokens,
            messages=[{"role": "user", "content": user_message}],
        )
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()

    def complete_json(
        self,
        role: str,
        user_message: str,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        text = self.complete(role, user_message, system=system, max_tokens=max_tokens)
        return _extract_json(text)


def _extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象."""
    # 1) 直接尝试
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) 剥离 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))

    # 3) 找第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError(f"模型输出里没有有效 JSON:\n{text[:500]}")
