"""Apify collector base class.

Apify is a third-party "scraping as a service" platform with anti-bot maintenance by their professional team.
We invoke their Actor via async REST API:
    POST https://api.apify.com/v2/acts/{actor}/runs?token=...   → start run, get run_id
    GET  https://api.apify.com/v2/actor-runs/{run_id}            → poll until SUCCEEDED
    GET  https://api.apify.com/v2/datasets/{dataset_id}/items    → fetch results

Using async avoids the 300-second hard limit of the sync endpoint (run-sync-get-dataset-items).

Each Actor has different input/output schemas, so the base class provides the framework
and platform subclasses implement:
    - _build_input(keywords, locations) -> dict
    - _parse_item(item: dict) -> CollectedJob
"""
from __future__ import annotations

import os
import time
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
        """Run Apify Actor asynchronously (start → poll → fetch), return list of raw items.

        Uses async API to avoid the 300-second hard limit of run-sync-get-dataset-items.
        Polls every 5 seconds until the run SUCCEEDS or FAILS (up to self._timeout_sec).
        """
        actor_path = self.actor_id.replace("/", "~")
        params = {"token": self._token}

        with httpx.Client(timeout=60) as client:
            # 1) Start the run
            start_resp = client.post(
                f"https://api.apify.com/v2/acts/{quote(actor_path)}/runs",
                params=params,
                json=input_data,
            )
            if start_resp.status_code >= 400:
                raise ApifyError(
                    f"Apify Actor {self.actor_id} start failed {start_resp.status_code}: {start_resp.text[:300]}"
                )
            run_data = start_resp.json().get("data", {})
            run_id = run_data.get("id")
            if not run_id:
                raise ApifyError(f"Apify did not return run id: {start_resp.text[:200]}")

            # 2) Poll until finished
            deadline = time.time() + self._timeout_sec
            poll_interval = 5
            while time.time() < deadline:
                time.sleep(poll_interval)
                poll_resp = client.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}",
                    params=params,
                )
                if poll_resp.status_code >= 400:
                    raise ApifyError(f"Apify poll failed {poll_resp.status_code}: {poll_resp.text[:200]}")
                status = poll_resp.json().get("data", {}).get("status", "")
                if status == "SUCCEEDED":
                    dataset_id = poll_resp.json()["data"]["defaultDatasetId"]
                    break
                if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                    raise ApifyError(f"Apify Actor run {status}: run_id={run_id}")
            else:
                raise ApifyError(f"Apify Actor {self.actor_id} timed out after {self._timeout_sec}s")

            # 3) Fetch results
            items_resp = client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                params={**params, "format": "json", "clean": "true"},
            )
            if items_resp.status_code >= 400:
                raise ApifyError(f"Apify dataset fetch failed {items_resp.status_code}: {items_resp.text[:200]}")
            try:
                items = items_resp.json()
            except Exception:
                raise ApifyError(f"Apify returned non-JSON: {items_resp.text[:200]}")
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
