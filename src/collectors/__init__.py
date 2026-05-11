"""岗位采集器: 各平台一个模块,统一接口.

每个平台的 backend 在 config.yaml 里 collectors.{platform}.backend 指定.

平台 / backend 矩阵 (✓ 已实装):
                playwright  apify      其他
    linkedin    ✓           ✓
    indeed      ✓           ✓
    glassdoor   ✓           -
    ziprecruiter ✓ (禁用)    -
    yc          -           ✓
    wellfound   -           ✓
    dice        -           ✓
    hackernews  -           -          ✓ (直连 HN Firebase + Algolia API)
"""
from .base import BaseCollector, CollectedJob
from .dice_apify import DiceApifyCollector
from .glassdoor import GlassdoorCollector
from .hackernews import HackerNewsHiringCollector
from .indeed import IndeedCollector
from .indeed_apify import IndeedApifyCollector
from .linkedin import LinkedInCollector
from .linkedin_apify import LinkedInApifyCollector
from .wellfound_apify import WellfoundApifyCollector
from .yc_apify import YCApifyCollector
from .ziprecruiter import ZipRecruiterCollector

__all__ = [
    "BaseCollector", "CollectedJob",
    "LinkedInCollector", "LinkedInApifyCollector",
    "IndeedCollector", "IndeedApifyCollector",
    "GlassdoorCollector",
    "ZipRecruiterCollector",
    "YCApifyCollector",
    "WellfoundApifyCollector",
    "DiceApifyCollector",
    "HackerNewsHiringCollector",
    "get_collector",
]


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
    },
    "ziprecruiter": {
        "playwright": ZipRecruiterCollector,
    },
    "yc": {
        "apify": YCApifyCollector,
    },
    "wellfound": {
        "apify": WellfoundApifyCollector,
    },
    "dice": {
        "apify": DiceApifyCollector,
    },
    "hackernews": {
        "api": HackerNewsHiringCollector,    # 直接 HN API,不走 apify/playwright
    },
}


def get_collector(name: str, config) -> BaseCollector:
    name = name.lower()
    aliases = {"zip": "ziprecruiter", "zr": "ziprecruiter", "hn": "hackernews",
               "ycombinator": "yc", "ang": "wellfound", "angellist": "wellfound"}
    name = aliases.get(name, name)

    if name not in _REGISTRY:
        raise ValueError(f"未知平台: {name}. 支持: {list(_REGISTRY)}")

    settings = config.collectors.get(name, {}) or {}
    # 默认 backend: apify 优先,否则取该平台第一个可用 backend
    available = _REGISTRY[name]
    default_backend = "apify" if "apify" in available else next(iter(available))
    backend = (settings.get("backend") or default_backend).lower()

    if backend not in available:
        raise ValueError(
            f"{name} 不支持 backend={backend}. 可用: {list(available)}"
        )

    return available[backend](config)
