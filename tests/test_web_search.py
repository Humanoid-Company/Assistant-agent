"""Unit tests for controlled web_search tool (mocked provider / network)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import httpx

from agents.types import AgentResult
from integrations.web_search import (
    HARD_MAX_RESULTS,
    FakeWebSearchProvider,
    TavilyWebSearchProvider,
    WebSearchHit,
    WebSearchRateLimiter,
    clamp_max_results,
    normalize_query,
    search_web,
)
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.gmail_tools import GmailToolWrappers
from tools.live_schemas import LIVE_BACKEND_TOOLS
from tools.notes_tools import NotesToolWrappers


def test_clamp_max_results():
    assert clamp_max_results(None) == 5
    assert clamp_max_results(3) == 3
    assert clamp_max_results(50) == HARD_MAX_RESULTS
    assert clamp_max_results(0) == 1
    assert clamp_max_results("nope") == 5


def test_normalize_query_and_empty():
    assert normalize_query("  hello   world ") == "hello world"
    result = search_web("", provider=FakeWebSearchProvider())
    assert result.error == "empty_query"
    assert result.results == []


def test_normal_search_returns_structured_hits():
    provider = FakeWebSearchProvider(
        [
            WebSearchHit(
                title="OpenAI news",
                url="https://example.com/a",
                snippet="Something new",
                source="example.com",
                published_at="2026-09-30",
            )
        ]
    )
    result = search_web(
        "OpenAI latest news",
        max_results=5,
        recency_days=1,
        provider=provider,
        rate_limiter=WebSearchRateLimiter(max_per_turn=10),
    )
    assert result.error is None
    assert result.query == "OpenAI latest news"
    assert len(result.results) == 1
    assert result.to_dict()["results"][0]["url"] == "https://example.com/a"
    assert provider.calls[0]["recency_days"] == 1
    assert provider.calls[0]["max_results"] == 5


def test_max_results_clamped_above_hard_max():
    provider = FakeWebSearchProvider(
        [WebSearchHit(title=f"t{i}", url=f"https://x/{i}", snippet="s") for i in range(20)]
    )
    result = search_web(
        "q",
        max_results=99,
        provider=provider,
        rate_limiter=WebSearchRateLimiter(max_per_turn=10),
    )
    assert provider.calls[0]["max_results"] == HARD_MAX_RESULTS
    assert len(result.results) == HARD_MAX_RESULTS


def test_timeout_and_provider_error():
    timeout_p = FakeWebSearchProvider()
    timeout_p.fail_with = "timeout"
    r1 = search_web("q", provider=timeout_p, rate_limiter=WebSearchRateLimiter(max_per_turn=10))
    assert r1.error == "web_search_timeout"
    assert r1.results == []

    bad = FakeWebSearchProvider()
    bad.fail_with = "unavailable"
    r2 = search_web("q", provider=bad, rate_limiter=WebSearchRateLimiter(max_per_turn=10))
    assert r2.error == "web_search_unavailable"


def test_empty_provider_results():
    result = search_web(
        "obscure",
        provider=FakeWebSearchProvider([]),
        rate_limiter=WebSearchRateLimiter(max_per_turn=10),
    )
    assert result.error is None
    assert result.results == []


def test_rate_limit_per_delegation():
    limiter = WebSearchRateLimiter(max_per_turn=2)
    provider = FakeWebSearchProvider([WebSearchHit("t", "https://x", "s")])
    assert search_web("a", provider=provider, rate_limiter=limiter, delegation_id="d1").error is None
    assert search_web("b", provider=provider, rate_limiter=limiter, delegation_id="d1").error is None
    limited = search_web("c", provider=provider, rate_limiter=limiter, delegation_id="d1")
    assert limited.error == "web_search_rate_limited"
    # New delegation resets.
    assert search_web("d", provider=provider, rate_limiter=limiter, delegation_id="d2").error is None


def test_tavily_parsing_and_http_errors():
    provider = TavilyWebSearchProvider("fake-key")

    class Resp:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {
                        "title": "FastAPI",
                        "url": "https://fastapi.tiangolo.com/",
                        "content": "Latest docs",
                        "published_date": "2026-01-01",
                    }
                ]
            }

    with patch("integrations.web_search.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.return_value = Resp()
        client_cls.return_value = client
        result = provider.search("FastAPI", max_results=5, recency_days=7, timeout_s=5)
    assert result.error is None
    assert result.results[0].title == "FastAPI"
    assert result.results[0].source == "fastapi.tiangolo.com"
    assert result.results[0].published_at == "2026-01-01"

    with patch("integrations.web_search.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = httpx.TimeoutException("slow")
        client_cls.return_value = client
        timed = provider.search("x", max_results=3, recency_days=None, timeout_s=1)
    assert timed.error == "web_search_timeout"

    with patch("integrations.web_search.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False

        class Bad:
            status_code = 503

            def json(self):
                return {}

        client.post.return_value = Bad()
        client_cls.return_value = client
        unavailable = provider.search("x", max_results=3, recency_days=None, timeout_s=1)
    assert unavailable.error == "web_search_unavailable"


def test_missing_api_key_without_provider():
    result = search_web(
        "q",
        api_key="",
        rate_limiter=WebSearchRateLimiter(max_per_turn=10),
    )
    assert result.error == "web_search_unavailable"


def test_live_schema_includes_web_search():
    tool = next(t for t in LIVE_BACKEND_TOOLS if t["name"] == "web_search")
    props = tool["parameters"]["properties"]
    assert "query" in props
    assert "max_results" in props
    assert "recency_days" in props
    assert tool["parameters"]["required"] == ["query"]


def test_executor_registers_web_search_offloaded():
    provider = FakeWebSearchProvider(
        [WebSearchHit("T", "https://example.com", "snippet about OpenAI")]
    )
    limiter = WebSearchRateLimiter(max_per_turn=5)

    def handler(args, context):
        result = search_web(
            str(args.get("query") or ""),
            max_results=args.get("max_results"),
            recency_days=args.get("recency_days"),
            provider=provider,
            rate_limiter=limiter,
            session_id=context.session_id,
            delegation_id=context.delegation_id,
        )
        from tools.results import ToolResult

        return ToolResult(
            ok=result.error is None,
            status="ok" if result.error is None else "error",
            message="ok" if result.results else (result.error or "empty"),
            data=result.to_dict(),
        )

    ex = ToolExecutor(
        calendar=CalendarToolWrappers(lambda **kw: AgentResult("success", "x")),
        gmail=GmailToolWrappers(lambda **kw: AgentResult("success", "x")),
        notes=NotesToolWrappers(lambda **kw: AgentResult("success", "x")),
    )
    ex.register("web_search", handler, run_in_thread=True)
    assert ex.is_offloaded("web_search")
    out = asyncio.run(
        ex.execute(
            "web_search",
            {"query": "OpenAI", "max_results": 3, "recency_days": 1},
            ToolExecutionContext(session_id="s1", delegation_id="del-1"),
        )
    )
    assert out.ok
    assert out.data["results"][0]["title"] == "T"


def test_realtime_tools_include_web_search():
    from assistant import TOOLS

    names = {t["name"] for t in TOOLS}
    assert "web_search" in names
