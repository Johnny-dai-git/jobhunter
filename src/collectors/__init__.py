"""Job collectors: one module per platform, unified interface.

Each platform's backend is specified in config.yaml as collectors.{platform}.backend.

Platform / backend matrix (✓ implemented):
                playwright  apify      other
    linkedin    ✓           ✓
    indeed      ✓           ✓
    glassdoor   ✓           -
    ziprecruiter ✓ (disabled) -
    yc          -           ✓
    wellfound   -           ✓
    dice        -           ✓
    hackernews  -           -          ✓ (direct HN Firebase + Algolia API)
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
        "api": HackerNewsHiringCollector,    # Direct HN API, not through apify/playwright
    },
}


def get_collector(name: str, config) -> BaseCollector:
    name = name.lower()
    aliases = {"zip": "ziprecruiter", "zr": "ziprecruiter", "hn": "hackernews",
               "ycombinator": "yc", "ang": "wellfound", "angellist": "wellfound"}
    name = aliases.get(name, name)

    if name not in _REGISTRY:
        raise ValueError(f"Unknown platform: {name}. Supported: {list(_REGISTRY)}")

    settings = config.collectors.get(name, {}) or {}
    # Default backend: apify first, otherwise use first available backend for platform
    available = _REGISTRY[name]
    default_backend = "apify" if "apify" in available else next(iter(available))
    backend = (settings.get("backend") or default_backend).lower()

    if backend not in available:
        raise ValueError(
            f"{name} does not support backend={backend}. Available: {list(available)}"
        )

    return available[backend](config)
