"""Collector base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..config import Config


@dataclass
class CollectedJob:
    """Collected job (intermediate structure before storing in database)."""

    source: str
    url: str
    title: str
    company: str
    external_id: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    description: Optional[str] = None
    extras: dict = field(default_factory=dict)


class BaseCollector(ABC):
    """All collectors implement this interface."""

    name: str = "base"

    def __init__(self, config: Config):
        self.config = config
        self._settings = config.collectors.get(self.name, {})

    @property
    def enabled(self) -> bool:
        return bool(self._settings.get("enabled", False))

    @property
    def max_per_run(self) -> int:
        return int(self._settings.get("max_per_run", 30))

    @property
    def cookie_file(self) -> Optional[str]:
        return self._settings.get("cookie_file")

    @abstractmethod
    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        """Search for jobs by keywords and locations, yield them one by one."""
        ...
