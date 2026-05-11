"""Apify 采集器基类.

Apify 是第三方"爬虫即服务"平台,反爬由他们专业团队维护.
我们通过 REST API 调他们的 Actor:
    POST https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token=...

每个 Actor 输入和输出 schema 不一样,所以基类提供框架,平台子类各自实现:
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
    """所有 Apify-backed 采集器的基类."""

    name = "apify-base"  # 子类必须覆盖

    def __init__(self, config: Config):
        super().__init__(config)
        apify_cfg = config.raw.get("apify", {}) or {}
        token_env = apify_cfg.get("api_token_env", "APIFY_API_TOKEN")
        self._token = os.getenv(token_env)
        self._timeout_sec = int(apify_cfg.get("default_timeout_sec", 600))
        # 平台配的 apify 子段
        self._apify_cfg = (self._settings.get("apify") or {})

    @property
    def actor_id(self) -> str:
        actor = self._apify_cfg.get("actor")
        if not actor:
            raise ApifyError(
                f"collectors.{self.name}.apify.actor 未配置. "
                f"去 https://apify.com/store 找到你想用的 Actor,把它 username/actorname 填进来."
            )
        return actor

    @property
    def input_overrides(self) -> dict:
        return self._apify_cfg.get("input_overrides") or {}

    # ---- 子类实现 ----
    def _build_input(self, keywords: list[str], locations: list[str]) -> dict:
        """返回 actor 需要的 input JSON. 子类必须实现."""
        raise NotImplementedError

    def _parse_item(self, item: dict) -> Optional[CollectedJob]:
        """把 actor 返回的一条数据映射成 CollectedJob. 子类必须实现.

        返回 None 表示这条数据应该被丢弃 (比如缺关键字段).
        """
        raise NotImplementedError

    # ---- 公共 search 流程 ----
    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        if not self._token:
            raise ApifyError(
                "APIFY_API_TOKEN 未设置. 去 https://console.apify.com/account/integrations 拿一个,填到 .env"
            )

        input_data = self._build_input(keywords, locations)
        # 用户的覆盖项最终生效
        input_data.update(self.input_overrides)

        # actor_id 形如 "username/actor-name", URL 里转成 "username~actor-name"
        actor_path = self.actor_id.replace("/", "~")
        # quote 一下,虽然一般是 ascii
        url = (
            f"https://api.apify.com/v2/acts/{quote(actor_path)}"
            "/run-sync-get-dataset-items"
        )
        params = {"token": self._token}

        print(f"  [apify] POST {self.actor_id}  input={input_data!r}")

        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(url, params=params, json=input_data)
        except httpx.TimeoutException:
            raise ApifyError(f"Apify Actor {self.actor_id} 超时 ({self._timeout_sec}s)")
        except httpx.HTTPError as e:
            raise ApifyError(f"Apify API 请求失败: {e}")

        if resp.status_code >= 400:
            try:
                msg = resp.json()
            except Exception:
                msg = resp.text[:500]
            raise ApifyError(
                f"Apify Actor {self.actor_id} 返回 {resp.status_code}: {msg}"
            )

        try:
            items = resp.json()
        except Exception as e:
            raise ApifyError(f"Apify 返回非 JSON: {resp.text[:200]}")

        if not isinstance(items, list):
            raise ApifyError(
                f"Apify Actor 返回了非数组: {type(items).__name__}. 检查 actor 是否正确."
            )

        print(f"  [apify] 拿回 {len(items)} 条原始数据")
        count = 0
        for item in items:
            if count >= self.max_per_run:
                break
            try:
                cj = self._parse_item(item)
            except Exception as e:
                print(f"  [apify] 解析失败,跳过: {e}")
                continue
            if cj is not None:
                yield cj
                count += 1
