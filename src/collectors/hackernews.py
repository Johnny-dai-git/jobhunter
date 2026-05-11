"""HackerNews 'Ask HN: Who is hiring?' 月度帖抓取.

完全免费,不需要 Apify. 用 HN 自家的 Algolia + Firebase API.

工作流:
1. Algolia 搜索最新的 "Ask HN: Who is hiring?" 主题帖,拿到 thread id
2. Firebase API 拉这个 thread 的所有 top-level 评论
3. 每条评论按 HN 惯例: 第一行是 'Company | Role | Location | Tech | Contact'
4. 用简单 heuristic 解析,无法解析就 yield 原文 (matcher 能容忍)

API 文档:
- https://hn.algolia.com/api  (search)
- https://github.com/HackerNews/API  (Firebase)
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional

import httpx

from .base import BaseCollector, CollectedJob


HN_ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_FIREBASE_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"


def _strip_html(html: str) -> str:
    """简单去 HTML 标签 + 解 entity."""
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_header_line(line: str) -> dict:
    """HN 惯例: 第一行像 'Anthropic | ML Infra Engineer | Remote/SF | Python, PyTorch'

    返回 {company, title, location} (任何字段都可能缺).
    """
    parts = [p.strip() for p in re.split(r"[|·•·]", line) if p.strip()]
    if not parts:
        return {}
    out: dict = {"company": parts[0]}
    if len(parts) > 1:
        out["title"] = parts[1]
    if len(parts) > 2:
        # 在剩下的里找像 location 的
        for p in parts[2:]:
            if re.search(r"remote|hybrid|\b(SF|NYC|US|EU|UK|CA|NY)\b|[A-Z][a-z]+ ?,? ?[A-Z]+", p):
                out["location"] = p
                break
    return out


def _find_latest_thread(keyword_filter: list[str] | None = None) -> Optional[dict]:
    """从 Algolia 找最近的 'Ask HN: Who is hiring?' story."""
    params = {
        "query": "Ask HN: Who is hiring?",
        "tags": "story,author_whoishiring",
        "hitsPerPage": 5,
    }
    resp = httpx.get(HN_ALGOLIA_SEARCH, params=params, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("hits") or []
    # 取最新一条 (Algolia 默认按时间)
    for hit in hits:
        title = hit.get("title") or ""
        if "Ask HN" in title and "hiring" in title.lower() and "wants" not in title.lower():
            return hit
    return hits[0] if hits else None


def _fetch_item(item_id: int) -> dict:
    resp = httpx.get(HN_FIREBASE_ITEM.format(id=item_id), timeout=20)
    resp.raise_for_status()
    return resp.json() or {}


class HackerNewsHiringCollector(BaseCollector):
    """从 HN 月度 'Who is hiring?' 帖抓取岗位."""

    name = "hackernews"

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        keywords_lower = {k.lower() for k in (keywords or [])}

        thread = _find_latest_thread()
        if not thread:
            print("  [hn] 没找到 Who is hiring 主题帖")
            return

        thread_id = int(thread["objectID"])
        thread_url = f"https://news.ycombinator.com/item?id={thread_id}"
        thread_title = thread.get("title") or "Ask HN: Who is hiring?"
        print(f"  [hn] 解析 {thread_title} (id={thread_id})")

        thread_full = _fetch_item(thread_id)
        kids = thread_full.get("kids") or []
        print(f"  [hn] thread 有 {len(kids)} 条 top-level 评论")

        count = 0
        for kid_id in kids:
            if count >= self.max_per_run:
                break
            try:
                item = _fetch_item(kid_id)
            except Exception as e:
                print(f"  [hn] fetch {kid_id} 失败: {e}")
                continue
            if not item or item.get("deleted") or item.get("dead"):
                continue
            text = _strip_html(item.get("text") or "")
            if not text or len(text) < 40:
                continue

            # 关键词过滤 (任何 keyword 命中就放过)
            text_lower = text.lower()
            if keywords_lower and not any(k in text_lower for k in keywords_lower):
                continue

            first_line = text.split(".")[0][:200]
            parsed = _parse_header_line(first_line)
            company = parsed.get("company") or "Unknown (HN)"
            title = parsed.get("title") or "(see post)"
            location = parsed.get("location")

            yield CollectedJob(
                source="hackernews",
                external_id=str(kid_id),
                url=f"https://news.ycombinator.com/item?id={kid_id}",
                title=title,
                company=company,
                location=location,
                description=text[:4000],
                extras={"thread_id": thread_id, "thread_url": thread_url},
            )
            count += 1
