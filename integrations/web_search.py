"""Controlled public web search for the Live voice agent (no arbitrary HTTP)."""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 5
HARD_MAX_RESULTS = 10
MAX_QUERY_LENGTH = 800
DEFAULT_TIMEOUT_S = 9.0
MAX_CALLS_PER_TURN = 3
# Fallback turn window when delegation_id is absent (Realtime / missing context).
_TURN_WINDOW_S = 45.0

_TAVILY_URL = "https://api.tavily.com/search"


@dataclass(frozen=True)
class WebSearchHit:
    title: str
    url: str
    snippet: str
    source: str = ""
    published_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WebSearchResult:
    query: str
    results: list[WebSearchHit] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": [r.to_dict() for r in self.results],
            "error": self.error,
        }


class WebSearchProvider(Protocol):
    def search(
        self,
        query: str,
        *,
        max_results: int,
        recency_days: int | None,
        timeout_s: float,
    ) -> WebSearchResult: ...


class WebSearchRateLimiter:
    """Limits search calls per Live delegation (preferred) or short session window."""

    def __init__(self, *, max_per_turn: int = MAX_CALLS_PER_TURN) -> None:
        self._max = max_per_turn
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._window_started: dict[str, float] = {}

    def allow(self, *, delegation_id: str | None, session_id: str | None) -> bool:
        if delegation_id:
            key = f"delegation:{delegation_id}"
            with self._lock:
                n = self._counts.get(key, 0)
                if n >= self._max:
                    return False
                self._counts[key] = n + 1
                return True
        # Fallback: sliding window per session.
        sid = session_id or "global"
        key = f"session:{sid}"
        now = time.monotonic()
        with self._lock:
            started = self._window_started.get(key)
            if started is None or (now - started) > _TURN_WINDOW_S:
                self._window_started[key] = now
                self._counts[key] = 0
            n = self._counts.get(key, 0)
            if n >= self._max:
                return False
            self._counts[key] = n + 1
            return True

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._window_started.clear()


_rate_limiter = WebSearchRateLimiter()


def get_web_search_rate_limiter() -> WebSearchRateLimiter:
    return _rate_limiter


def clamp_max_results(value: int | None) -> int:
    try:
        n = int(value) if value is not None else DEFAULT_MAX_RESULTS
    except (TypeError, ValueError):
        n = DEFAULT_MAX_RESULTS
    if n < 1:
        n = 1
    return min(n, HARD_MAX_RESULTS)


def normalize_query(query: str | None) -> str:
    return re.sub(r"\s+", " ", (query or "").strip())


def _source_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


class TavilyWebSearchProvider:
    """Tavily Search API — async-friendly via httpx (sync client for thread offload)."""

    def __init__(self, api_key: str, *, endpoint: str = _TAVILY_URL) -> None:
        self._api_key = api_key
        self._endpoint = endpoint

    def search(
        self,
        query: str,
        *,
        max_results: int,
        recency_days: int | None,
        timeout_s: float,
    ) -> WebSearchResult:
        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
        }
        if recency_days is not None and recency_days > 0:
            payload["days"] = int(recency_days)
        try:
            with httpx.Client(timeout=timeout_s) as client:
                response = client.post(self._endpoint, json=payload)
        except httpx.TimeoutException:
            logger.warning("WEB_SEARCH timeout")
            return WebSearchResult(query=query, results=[], error="web_search_timeout")
        except httpx.HTTPError as exc:
            logger.warning("WEB_SEARCH provider_error type=%s", type(exc).__name__)
            return WebSearchResult(query=query, results=[], error="web_search_unavailable")

        if response.status_code >= 400:
            logger.warning(
                "WEB_SEARCH provider_error status=%s",
                response.status_code,
            )
            return WebSearchResult(query=query, results=[], error="web_search_unavailable")

        try:
            data = response.json()
        except ValueError:
            logger.warning("WEB_SEARCH provider_error invalid_json")
            return WebSearchResult(query=query, results=[], error="web_search_unavailable")

        hits: list[WebSearchHit] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("content") or item.get("snippet") or "").strip()
            if not (url or title or snippet):
                continue
            published = item.get("published_date") or item.get("published_at")
            published_at = str(published).strip() if published else None
            hits.append(
                WebSearchHit(
                    title=title or url,
                    url=url,
                    snippet=snippet,
                    source=_source_from_url(url),
                    published_at=published_at or None,
                )
            )
            if len(hits) >= max_results:
                break
        return WebSearchResult(query=query, results=hits, error=None)


class FakeWebSearchProvider:
    """Deterministic provider for unit tests."""

    def __init__(self, hits: list[WebSearchHit] | None = None) -> None:
        self.hits = list(hits or [])
        self.calls: list[dict[str, Any]] = []
        self.fail_with: str | None = None  # timeout | unavailable

    def search(
        self,
        query: str,
        *,
        max_results: int,
        recency_days: int | None,
        timeout_s: float,
    ) -> WebSearchResult:
        self.calls.append(
            {
                "query": query,
                "max_results": max_results,
                "recency_days": recency_days,
                "timeout_s": timeout_s,
            }
        )
        if self.fail_with == "timeout":
            return WebSearchResult(query=query, results=[], error="web_search_timeout")
        if self.fail_with == "unavailable":
            return WebSearchResult(query=query, results=[], error="web_search_unavailable")
        return WebSearchResult(query=query, results=self.hits[:max_results], error=None)


def search_web(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    recency_days: int | None = None,
    provider: WebSearchProvider | None = None,
    api_key: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    delegation_id: str | None = None,
    session_id: str | None = None,
    rate_limiter: WebSearchRateLimiter | None = None,
) -> WebSearchResult:
    """Sync search entry used from Live/Realtime worker threads."""
    q = normalize_query(query)
    if not q:
        return WebSearchResult(query="", results=[], error="empty_query")
    if len(q) > MAX_QUERY_LENGTH:
        q = q[:MAX_QUERY_LENGTH].rstrip()

    capped = clamp_max_results(max_results)
    days = None
    if recency_days is not None:
        try:
            days = int(recency_days)
        except (TypeError, ValueError):
            days = None
        if days is not None and days <= 0:
            days = None

    limiter = rate_limiter or _rate_limiter
    if not limiter.allow(delegation_id=delegation_id, session_id=session_id):
        logger.info(
            "WEB_SEARCH rate_limited session_id=%s delegation_id=%s",
            session_id,
            delegation_id,
        )
        return WebSearchResult(query=q, results=[], error="web_search_rate_limited")

    active = provider
    if active is None:
        key = (api_key or "").strip()
        if not key:
            logger.warning("WEB_SEARCH provider_error missing_api_key")
            return WebSearchResult(query=q, results=[], error="web_search_unavailable")
        active = TavilyWebSearchProvider(key)

    logger.info(
        'WEB_SEARCH query="%s" max_results=%s recency_days=%s',
        q[:120],
        capped,
        days,
    )
    started = time.monotonic()
    result = active.search(q, max_results=capped, recency_days=days, timeout_s=timeout_s)
    duration_ms = int((time.monotonic() - started) * 1000)
    if result.error:
        logger.info(
            "WEB_SEARCH completed results=0 duration_ms=%s error=%s",
            duration_ms,
            result.error,
        )
    else:
        logger.info(
            "WEB_SEARCH completed results=%s duration_ms=%s",
            len(result.results),
            duration_ms,
        )
    return result


async def search_web_async(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    recency_days: int | None = None,
    provider: WebSearchProvider | None = None,
    api_key: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    delegation_id: str | None = None,
    session_id: str | None = None,
    rate_limiter: WebSearchRateLimiter | None = None,
) -> WebSearchResult:
    """Async wrapper — offloads the sync provider to a worker thread."""
    import asyncio

    return await asyncio.to_thread(
        search_web,
        query,
        max_results=max_results,
        recency_days=recency_days,
        provider=provider,
        api_key=api_key,
        timeout_s=timeout_s,
        delegation_id=delegation_id,
        session_id=session_id,
        rate_limiter=rate_limiter,
    )
