"""岗位采集器: 各平台一个模块,统一接口.

每个平台都有 2 个 backend 可选 (config.yaml 里 collectors.{platform}.backend):
- playwright: 自己用 Playwright 爬 (免费,容易遇到 Cloudflare)
- apify: 走 Apify Actor (付费,稳)

目前实装情况:
- LinkedIn: playwright + apify
- Indeed: playwright (apify 占位待加)
- Glassdoor: playwright (同上)
- ZipRecruiter: playwright (同上)
"""
from .base import BaseCollector, CollectedJob
from .glassdoor import GlassdoorCollector
from .indeed import IndeedCollector
from .indeed_apify import IndeedApifyCollector
from .linkedin import LinkedInCollector
from .linkedin_apify import LinkedInApifyCollector
from .ziprecruiter import ZipRecruiterCollector

__all__ = [
    "BaseCollector",
    "CollectedJob",
    "LinkedInCollector",
    "LinkedInApifyCollector",
    "IndeedCollector",
    "IndeedApifyCollector",
    "GlassdoorCollector",
    "ZipRecruiterCollector",
    "get_collector",
]


# 平台 -> 各 backend 实现
_REGISTRY: dict[str, dict[str, type[BaseCollector]]] = {
    "linkedin": {
        "playwright": LinkedInCollector,
        "apify": LinkedInApifyCollector,
    },
    "indeed": {
        "playwright": IndeedCollector,
        "apify": IndeedApifyCollector,
    },
    "glassdoor": {
        "playwright": GlassdoorCollector,
        # "apify": GlassdoorApifyCollector,
    },
    "ziprecruiter": {
        "playwright": ZipRecruiterCollector,
        # "apify": ZipRecruiterApifyCollector,
    },
}


def get_collector(name: str, config) -> BaseCollector:
    name = name.lower()
    if name in ("zip", "zr"):
        name = "ziprecruiter"

    if name not in _REGISTRY:
        raise ValueError(f"未知平台: {name}. 支持: {list(_REGISTRY)}")

    settings = config.collectors.get(name, {}) or {}
    backend = (settings.get("backend") or "playwright").lower()

    if backend not in _REGISTRY[name]:
        raise ValueError(
            f"{name} 不支持 backend={backend}. "
            f"可用: {list(_REGISTRY[name].keys())}"
        )

    cls = _REGISTRY[name][backend]
    return cls(config)
