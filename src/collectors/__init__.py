"""岗位采集器: 各平台一个模块,统一接口."""
from .base import BaseCollector, CollectedJob
from .glassdoor import GlassdoorCollector
from .indeed import IndeedCollector
from .linkedin import LinkedInCollector
from .ziprecruiter import ZipRecruiterCollector

__all__ = [
    "BaseCollector",
    "CollectedJob",
    "LinkedInCollector",
    "IndeedCollector",
    "GlassdoorCollector",
    "ZipRecruiterCollector",
    "get_collector",
]


def get_collector(name: str, config) -> BaseCollector:
    name = name.lower()
    if name == "linkedin":
        return LinkedInCollector(config)
    if name == "indeed":
        return IndeedCollector(config)
    if name == "glassdoor":
        return GlassdoorCollector(config)
    if name in ("ziprecruiter", "zip", "zr"):
        return ZipRecruiterCollector(config)
    raise ValueError(f"未知采集器: {name}")
