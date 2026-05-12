"""Thin wrapper for LLM calls (Anthropic SDK for Claude, OpenAI SDK for DeepSeek)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from openai import OpenAI as _OpenAI

from .config import Config


_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def make_client(config: Config, role: str) -> tuple[Any, str, str]:
    """Return (client, model_name, provider) based on role.

    Provider=deepseek routes through OpenAI SDK to https://api.deepseek.com.
    Provider=claude (or others) routes through Anthropic SDK.
    """
    role_cfg = config.role_config(role)
    provider = role_cfg["provider"]
    model_name = role_cfg["name"]
    api_key = config.api_key_for(provider)
    settings = config.provider_settings(provider)

    if provider == "deepseek":
        # Use OpenAI SDK for DeepSeek API (OpenAI-compatible endpoint)
        client = _OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
    else:
        # Use Anthropic SDK for Claude and compatible providers
        base_url = settings.get("base_url") or None  # Treat empty string as None
        if base_url:
            client = Anthropic(api_key=api_key, base_url=base_url)
        else:
            client = Anthropic(api_key=api_key)

    return client, model_name, provider


def load_prompt(name: str) -> str:
    """Load template from prompts/<name>.md."""
    path = _PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def render(template: str, **kwargs: Any) -> str:
    """Lightweight template replacement: {{ name }} -> kwargs['name']. Intentionally avoid jinja2 to reduce cognitive load."""
    out = template
    for k, v in kwargs.items():
        out = out.replace(f"{{{{ {k} }}}}", str(v))
        out = out.replace(f"{{{{{k}}}}}", str(v))
    return out


def _convert_tool_to_openai(anthropic_tool: dict) -> dict:
    """Convert Anthropic tool schema to OpenAI format.

    Anthropic: {name, description, input_schema}
    OpenAI: {type: "function", function: {name, description, parameters: input_schema}}
    """
    return {
        "type": "function",
        "function": {
            "name": anthropic_tool["name"],
            "description": anthropic_tool["description"],
            "parameters": anthropic_tool["input_schema"],
        },
    }


def llm_complete(
    client: Any,
    model: str,
    provider: str,
    messages: list[dict],
    max_tokens: int,
    system: str | None = None,
) -> str:
    """Unified LLM completion handler for both Anthropic and OpenAI SDKs.

    Args:
        client: Anthropic or OpenAI client instance
        model: model name string
        provider: "claude", "deepseek", etc.
        messages: list of message dicts
        max_tokens: max tokens to generate
        system: system prompt (only for Anthropic format)

    Returns:
        Final text response
    """
    if provider == "deepseek":
        # OpenAI SDK: system goes in messages array
        if system:
            messages = [{"role": "system", "content": system}] + messages
        with client.chat.completions.stream(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            extra_body={"thinking": {"type": "disabled"}},
        ) as stream:
            resp = stream.get_final_completion()
        return resp.choices[0].message.content or ""
    else:
        # Anthropic SDK: system is top-level parameter
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        if system:
            kwargs["system"] = system
        with client.messages.stream(**kwargs) as _s:
            resp = _s.get_final_message()
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()


class ClaudeClient:
    """Convenient wrapper for single text completion. Name kept as ClaudeClient for backward compatibility,
    but actually routes to Claude or DeepSeek based on role."""

    def __init__(self, config: Config):
        self.config = config

    def complete(
        self,
        role: str,
        user_message: str,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        client, model, provider = make_client(self.config, role)
        return llm_complete(
            client,
            model,
            provider,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=max_tokens or self.config.max_tokens,
            system=system,
        )

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
    """Extract JSON object from model output."""
    # 1) Try directly
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) Strip ```json ... ``` fence
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))

    # 3) Find first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError(f"No valid JSON found in model output:\n{text[:500]}")
