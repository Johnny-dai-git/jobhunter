"""Apify collector base class.

Apify is a third-party "scraping as a service" platform with anti-bot maintenance by their professional team.
We invoke their Actor via REST API:
    POST https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token=...

Each Actor has different input/output schemas, so the base class provides the framework
and platform subclasses implement:
    - _build_input(keywords, locations) -> dict
    - _parse_item(item: dict) -> CollectedJob
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Optional
from urllib.parse import quote

import httpx

from ..config import Config
from .base import BaseCollector, CollectedJob


class ApifyError(RuntimeError):
    pass


class ApifyCollector(BaseCollector):
    """Base class for all Apify-backed collectors."""

    name = "apify-base"  # Subclasses must override

    def __init__(self, config: Config):
        super().__init__(config)
        apify_cfg = config.raw.get("apify", {}) or {}
        token_env = apify_cfg.get("api_token_env", "APIFY_API_TOKEN")
        self._token = os.getenv(token_env)
        self._timeout_sec = int(apify_cfg.get("default_timeout_sec", 600))
        # Platform-specific apify subsection
        self._apify_cfg = (self._settings.get("apify") or {})

    @property
    def actor_id(self) -> str:
        actor = self._apify_cfg.get("actor")
        if not actor:
            raise ApifyError(
                f"collectors.{self.name}.apify.actor not configured. "
                f"Find the Actor you want at https://apify.com/store and fill in username/actorname."
            )
        return actor

    @property
    def input_overrides(self) -> dict:
        return self._apify_cfg.get("input_overrides") or {}

    # ---- Subclass implementation ----
    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        """Return the input JSON needed by the actor. Subclasses must implement."""
        raise NotImplementedError

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        """Map an item returned by the actor to CollectedJob. Subclasses must implement.

        Return None if the item should be discarded (e.g., missing key fields).
        """
        raise NotImplementedError

    # ---- Public utility methods ----
    def _run_actor(self, input_data: dict) -> list[dict]:
        """Make one Apify Actor request, return list of raw items.
        Subclasses can call this directly in their search() for more flexible search strategies.
        """
        actor_path = self.actor_id.replace("/", "~")
        url = (
            f"https://api.apify.com/v2/acts/{quote(actor_path)}"
            "/run-sync-get-dataset-items"
        )
        params = {"token": self._token}

        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(url, params=params, json=input_data)
        except httpx.TimeoutException:
            raise ApifyError(f"Apify Actor {self.actor_id} timed out ({self._timeout_sec}s)")
        except httpx.HTTPError as e:
            raise ApifyError(f"Apify API request failed: {e}")

        if resp.status_code >= 400:
            try:
                msg = resp.json()
            except Exception:
                msg = resp.text[:500]
            raise ApifyError(
                f"Apify Actor {self.actor_id} returned {resp.status_code}: {msg}"
            )
        try:
            items = resp.json()
        except Exception:
            raise ApifyError(f"Apify returned non-JSON: {resp.text[:200]}")
        if not isinstance(items, list):
            raise ApifyError(
                f"Apify Actor returned non-array: {type(items).__name__}. Check if actor is correct."
            )
        return items

    # ---- Default search flow (subclasses can override) ----
    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        if not self._token:
            raise ApifyError(
                "APIFY_API_TOKEN not set. Get one at https://console.apify.com/account/integrations and add to .env"
            )

        input_data = self._build_input(keywords, locations)
        input_data.update(self.input_overrides)

        print(f"  [apify] POST {self.actor_id}  input={input_data!r}")
        items = self._run_actor(input_data)
        print(f"  [apify] received {len(items)} raw items")

        count = 0
        for item in items:
            if count >= self.max_per_run:
                break
            try:
                cj = self._parse_item(item)
            except Exception as e:
                print(f"  [apify] parse failed, skipping: {e}")
                continue
            if cj is not None:
                yield cj
                count += 1
