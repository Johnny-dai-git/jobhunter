"""HackerNews 'Ask HN: Who is hiring?' monthly thread scraper.

Completely free, no Apify needed. Uses HN's own Algolia + Firebase APIs.

Workflow:
1. Algolia search for latest "Ask HN: Who is hiring?" thread, get thread id
2. Firebase API fetch all top-level comments from that thread
3. By HN convention, each comment's first line is 'Company | Role | Location | Tech | Contact'
4. Parse with simple heuristics, yield raw text if unparseable (matcher can handle it)

API documentation:
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
    """Simple HTML tag removal + entity decoding."""
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_header_line(line: str) -> dict:
    """HN convention: first line looks like 'Anthropic | ML Infra Engineer | Remote/SF | Python, PyTorch'

    Returns {company, title, location} (any field may be missing).
    """
    parts = [p.strip() for p in re.split(r"[|·•·]", line) if p.strip()]
    if not parts:
        return {}
    out: dict = {"company": parts[0]}
    if len(parts) > 1:
        out["title"] = parts[1]
    if len(parts) > 2:
        # Look for location-like string in remaining parts
        for p in parts[2:]:
            if re.search(r"remote|hybrid|\b(SF|NYC|US|EU|UK|CA|NY)\b|[A-Z][a-z]+ ?,? ?[A-Z]+", p):
                out["location"] = p
                break
    return out


def _find_latest_thread(keyword_filter: list[str] | None = None) -> Optional[dict]:
    """Find latest 'Ask HN: Who is hiring?' story from Algolia."""
    params = {
        "query": "Ask HN: Who is hiring?",
        "tags": "story,author_whoishiring",
        "hitsPerPage": 5,
    }
    resp = httpx.get(HN_ALGOLIA_SEARCH, params=params, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("hits") or []
    # Get latest one (Algolia defaults to time-sorted)
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
    """Collect jobs from HN monthly 'Who is hiring?' thread."""

    name = "hackernews"

    def search(self, keywords: list[str], locations: list[str]) -> Iterable[CollectedJob]:
        keywords_lower = {k.lower() for k in (keywords or [])}

        thread = _find_latest_thread()
        if not thread:
            print("  [hn] could not find Who is hiring thread")
            return

        thread_id = int(thread["objectID"])
        thread_url = f"https://news.ycombinator.com/item?id={thread_id}"
        thread_title = thread.get("title") or "Ask HN: Who is hiring?"
        print(f"  [hn] parsing {thread_title} (id={thread_id})")

        thread_full = _fetch_item(thread_id)
        kids = thread_full.get("kids") or []
        print(f"  [hn] thread has {len(kids)} top-level comments")

        count = 0
        for kid_id in kids:
            if count >= self.max_per_run:
                break
            try:
                item = _fetch_item(kid_id)
            except Exception as e:
                print(f"  [hn] fetch {kid_id} failed: {e}")
                continue
            if not item or item.get("deleted") or item.get("dead"):
                continue
            text = _strip_html(item.get("text") or "")
            if not text or len(text) < 40:
                continue

            # Keyword filter (pass through if any keyword matches)
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
