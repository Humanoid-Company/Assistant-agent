"""Pure helpers for nested Responses delegation events (no network/audio)."""
from __future__ import annotations

from dataclasses import dataclass, field
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


# Nested Responses events that end a delegated response.
RESPONSE_FINISHED_TYPES = frozenset({"response.completed", "response.incomplete", "response.failed"})


@dataclass
class _ResponseState:
    calls: set[str] = field(default_factory=set)
    outstanding: set[str] = field(default_factory=set)
    finished: bool = False
    continued: bool = False


class DelegatedResponseTracker:
    """When to send `response.create` after tool results.

    A delegated response may emit several function calls (parallel_tool_calls). A fast tool can
    finish before the model has even emitted the next call, so «every known call has a result» is
    not enough: continue only once the response has finished AND every call it made has a result.
    output_item.done carries no response id, so calls are grouped by the Live delegation id.
    """

    def __init__(self) -> None:
        self._states: dict[str, _ResponseState] = {}

    def response_started(self, key: str) -> None:
        self._states[key] = _ResponseState()

    def call_started(self, key: str, call_id: str) -> None:
        state = self._states.setdefault(key, _ResponseState())
        state.calls.add(call_id)
        state.outstanding.add(call_id)

    def response_finished(self, key: str, *, ok: bool = True) -> bool:
        """True → continue now (results were already in)."""
        state = self._states.get(key)
        if state is None:
            return False
        if not ok:
            state.continued = True  # a failed/cut-off response is not continued
            return False
        state.finished = True
        return self._ready(state)

    def call_finished(self, key: str, call_id: str) -> bool:
        """True → continue now; False → wait for the response (or more results)."""
        state = self._states.get(key)
        if state is None:
            return True  # untracked call: continue as before
        state.outstanding.discard(call_id)
        return self._ready(state)

    def results_complete(self, key: str) -> bool:
        """Every call seen so far has a result (fallback when the finish event never comes)."""
        state = self._states.get(key)
        return state is not None and not state.outstanding and not state.continued and bool(state.calls)

    def mark_continued(self, key: str) -> None:
        state = self._states.get(key)
        if state is not None:
            state.continued = True

    @staticmethod
    def _ready(state: _ResponseState) -> bool:
        return state.finished and bool(state.calls) and not state.outstanding and not state.continued
