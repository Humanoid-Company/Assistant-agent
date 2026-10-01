"""`web_search` tool handler shared by the desktop assistant and the web server."""
from __future__ import annotations

from config import WEB_SEARCH_API_KEY, WEB_SEARCH_TIMEOUT_S
from integrations.web_search import WebSearchRateLimiter, search_web
from tools.results import ToolResult


def web_search_tool_result(
    args: dict,
    *,
    session_id: str | None = None,
    delegation_id: str | None = None,
    limiter: WebSearchRateLimiter | None = None,
) -> ToolResult:
    """Run the `web_search` tool and shape the result for the voice model."""
    query = str(args.get("query") or "")
    max_results = args.get("max_results")
    recency_days = args.get("recency_days")
    result = search_web(
        query,
        max_results=max_results if max_results is not None else 5,
        recency_days=recency_days if recency_days is not None else None,
        api_key=WEB_SEARCH_API_KEY,
        timeout_s=WEB_SEARCH_TIMEOUT_S,
        delegation_id=delegation_id,
        session_id=session_id,
        rate_limiter=limiter,
    )
    data = result.to_dict()
    if result.error == "empty_query":
        return ToolResult(
            ok=False,
            status="needs_more_info",
            message="Порожній пошуковий запит — уточни, що саме шукати.",
            data=data,
        )
    if result.error == "web_search_rate_limited":
        return ToolResult(
            ok=False,
            status="rate_limited",
            message="Забагато пошукових запитів підряд. Спершу озвуч те, що вже знайшов.",
            data=data,
        )
    if result.error == "web_search_timeout":
        return ToolResult(
            ok=False,
            status="error",
            message="Пошук в інтернеті не встиг відповісти. Спробуй коротший запит або пізніше.",
            data=data,
        )
    if result.error == "web_search_unavailable":
        return ToolResult(
            ok=False,
            status="error",
            message="Вебпошук зараз недоступний. Можу відповісти з того, що вже знаю, або спробуємо пізніше.",
            data=data,
        )
    if not result.results:
        return ToolResult(
            ok=True,
            status="ok",
            message="За цим запитом надійних результатів не знайдено.",
            data=data,
        )
    # Compact message for the model; structured hits live in data.results.
    lines = []
    for hit in result.results[:5]:
        bit = hit.title or hit.source or hit.url
        if hit.snippet:
            bit = f"{bit}: {hit.snippet[:220]}"
        lines.append(bit)
    return ToolResult(
        ok=True,
        status="ok",
        message="Знайдено результати пошуку. Коротко підсумуй користувачу; URL не зачитуй без прохання. "
        + " | ".join(lines),
        data=data,
    )
