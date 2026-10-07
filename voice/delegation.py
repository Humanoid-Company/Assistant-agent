"""Pure helpers for nested Responses delegation events (no network/audio)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def responses_delegation(
    *,
    model: str,
    instructions: str,
    tools: list[dict],
    parallel_tools: bool,
    effort: str = "",
) -> dict[str, Any]:
    """Live session `delegation` block — shared by the desktop session and the web backend."""
    responses: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "tools": tools,
        "tool_choice": "auto",
        "parallel_tool_calls": parallel_tools,
    }
    if effort:
        responses["reasoning"] = {"effort": effort}
    return {"type": "responses", "responses": responses}


def event_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass(frozen=True)
class CompletedFunctionCall:
    call_id: str
    name: str
    arguments: Any
    delegation_id: str | None
    response_id: str | None


def extract_completed_function_call(outer_event: Any) -> CompletedFunctionCall | None:
    """Return a function call only from nested response.output_item.done.

    Argument-delta events must NOT produce a CompletedFunctionCall.
    """
    if event_attr(outer_event, "type") != "response.event":
        return None
    inner = event_attr(outer_event, "event")
    if event_attr(inner, "type") != "response.output_item.done":
        return None
    item = event_attr(inner, "item")
    if event_attr(item, "type") != "function_call":
        return None
    call_id = event_attr(item, "call_id")
    name = event_attr(item, "name")
    if not call_id or not name:
        return None
    response_id = event_attr(inner, "response_id") or event_attr(event_attr(inner, "response"), "id")
    return CompletedFunctionCall(
        call_id=str(call_id),
        name=str(name),
        arguments=event_attr(item, "arguments"),
        delegation_id=(
            str(event_attr(outer_event, "delegation_id"))
            if event_attr(outer_event, "delegation_id") is not None
            else None
        ),
        response_id=str(response_id) if response_id else None,
    )


def accumulate_transcript(buffer: str, delta: str) -> str:
    """Append a transcript fragment; deltas are not complete sentences."""
    return buffer + (delta or "")
