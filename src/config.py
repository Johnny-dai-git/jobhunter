"""Config loading: read config.yaml and .env"""
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
            raise FileNotFoundError(f"Config file not found: {path}. Please create from template.")
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(raw=raw)

    # --- Convenience accessors ---
    @property
    def anthropic_api_key(self) -> str:
        """Backward compatible: old code may read this directly."""
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not found, please set in .env file or environment variable"
            )
        return key

    def providers(self) -> dict[str, dict[str, Any]]:
        return self.raw.get("providers", {}) or {}

    def role_config(self, role: str) -> dict[str, Any]:
        """Return {provider, name} config for a role.

        Supports two formats:
        - String: "claude-sonnet-4-6"  -> default provider=claude
        - Dict:   {provider: deepseek, name: deepseek-v4-flash}
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
        """Backward compatible: return only model name string."""
        return self.role_config(role)["name"]

    def provider_for(self, role: str) -> str:
        return self.role_config(role)["provider"]

    def provider_settings(self, provider: str) -> dict[str, Any]:
        provs = self.providers()
        if provider not in provs:
            raise KeyError(
                f"providers.{provider} not defined. Available: {list(provs.keys())}"
            )
        return provs[provider]

    def api_key_for(self, provider: str) -> str:
        s = self.provider_settings(provider)
        env_name = s.get("api_key_env", "ANTHROPIC_API_KEY")
        key = os.getenv(env_name)
        if not key:
            raise RuntimeError(
                f"{env_name} not found (required by provider {provider}), please set in .env"
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
        base = dict(self.raw.get("freshness", {}))
        # Web UI can override via data/settings.json
        try:
            import json as _json
            settings_path = self.project_root / "data" / "settings.json"
            if settings_path.exists():
                data = _json.loads(settings_path.read_text(encoding="utf-8"))
                v = data.get("freshness_hours")
                if v is not None:
                    base["max_age_hours"] = int(v)
        except Exception:
            pass
        return base

    @property
    def digest(self) -> dict[str, Any]:
        return self.raw.get("digest", {})

    @property
    def apply_settings(self) -> dict[str, Any]:
        return self.raw.get("apply", {})

    def env(self, key: str, default: str | None = None) -> str | None:
        return os.getenv(key, default)

    def path(self, key: str) -> Path:
        """Get a relative path from paths.*, return absolute path."""
        rel = self.raw.get("paths", {}).get(key)
        if not rel:
            raise KeyError(f"config.paths.{key} not configured")
        p = (self.project_root / rel).resolve()
        # Automatically create parent directories to avoid later errors
        if p.suffix:  # It's a file
            p.parent.mkdir(parents=True, exist_ok=True)
        else:
            p.mkdir(parents=True, exist_ok=True)
        return p
