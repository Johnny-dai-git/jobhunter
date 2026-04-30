"""配置加载: 读取 config.yaml 和 .env"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    project_root: Path = PROJECT_ROOT

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Config":
        load_dotenv(PROJECT_ROOT / ".env")
        path = Path(config_path) if config_path else PROJECT_ROOT / "config.yaml"
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}. 请基于模板创建.")
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(raw=raw)

    # --- 便捷访问 ---
    @property
    def anthropic_api_key(self) -> str:
        """向后兼容: 旧代码可能直接读这个."""
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "未找到 ANTHROPIC_API_KEY,请在 .env 文件或环境变量中设置"
            )
        return key

    def providers(self) -> dict[str, dict[str, Any]]:
        return self.raw.get("providers", {}) or {}

    def role_config(self, role: str) -> dict[str, Any]:
        """返回某 role 的 {provider, name} 配置.

        兼容两种写法:
        - 字符串: "claude-sonnet-4-6"  -> 默认 provider=claude
        - dict:   {provider: deepseek, name: deepseek-v4-flash}
        """
        models = self.raw.get("model", {}) or {}
        cfg = models.get(role)
        if isinstance(cfg, str):
            return {"provider": "claude", "name": cfg}
        if isinstance(cfg, dict) and "name" in cfg:
            return {"provider": cfg.get("provider", "claude"), "name": cfg["name"]}
        # fallback
        return {"provider": "claude", "name": "claude-sonnet-4-6"}

    def model(self, role: str) -> str:
        """向后兼容: 只返回模型名字符串."""
        return self.role_config(role)["name"]

    def provider_for(self, role: str) -> str:
        return self.role_config(role)["provider"]

    def provider_settings(self, provider: str) -> dict[str, Any]:
        provs = self.providers()
        if provider not in provs:
            raise KeyError(
                f"providers.{provider} 未定义.可用: {list(provs.keys())}"
            )
        return provs[provider]

    def api_key_for(self, provider: str) -> str:
        s = self.provider_settings(provider)
        env_name = s.get("api_key_env", "ANTHROPIC_API_KEY")
        key = os.getenv(env_name)
        if not key:
            raise RuntimeError(
                f"未找到 {env_name} (provider {provider} 需要),请在 .env 中设置"
            )
        return key

    @property
    def max_tokens(self) -> int:
        return int(self.raw.get("model", {}).get("max_tokens", 4096))

    @property
    def preferences(self) -> dict[str, Any]:
        return self.raw.get("preferences", {})

    @property
    def scoring(self) -> dict[str, Any]:
        return self.raw.get("scoring", {})

    @property
    def collectors(self) -> dict[str, Any]:
        return self.raw.get("collectors", {})

    @property
    def freshness(self) -> dict[str, Any]:
        return self.raw.get("freshness", {})

    @property
    def digest(self) -> dict[str, Any]:
        return self.raw.get("digest", {})

    @property
    def apply_settings(self) -> dict[str, Any]:
        return self.raw.get("apply", {})

    def env(self, key: str, default: str | None = None) -> str | None:
        return os.getenv(key, default)

    def path(self, key: str) -> Path:
        """从 paths.* 中取一个相对路径,返回绝对路径."""
        rel = self.raw.get("paths", {}).get(key)
        if not rel:
            raise KeyError(f"config.paths.{key} 未配置")
        p = (self.project_root / rel).resolve()
        # 自动建好父目录,避免后续报错
        if p.suffix:  # 是文件
            p.parent.mkdir(parents=True, exist_ok=True)
        else:
            p.mkdir(parents=True, exist_ok=True)
        return p
