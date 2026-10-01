"""Thin Notes wrappers around NotesAgent / AgentRouter."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agents.types import AgentResult

_NOTES_FIELDS = (
    "content",
    "title",
    "category",
    "query",
    "target",
    "note_id",
    "append_text",
    "limit",
    "date_filter",
    "date",
)


def notes_fields(args: dict[str, Any]) -> dict[str, Any]:
    return {
        key: args[key]
        for key in _NOTES_FIELDS
        if key in args and args[key] is not None and key not in {"user_sub", "email", "session_id"}
    }


class NotesToolWrappers:
    """Model-facing notes ops that delegate to AgentRouter.notes_action."""

    def __init__(self, notes_action: Callable[..., AgentResult]) -> None:
        self._notes_action = notes_action

    def _call(self, action: str, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        kwargs = notes_fields(args)
        kwargs["action"] = action
        if session_id:
            kwargs["session_id"] = session_id
        return self._notes_action(**kwargs)

    def add_note(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("add", args, session_id=session_id)

    def read_notes(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("read", args, session_id=session_id)

    def search_notes(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("search", args, session_id=session_id)

    def count_notes(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("count", args, session_id=session_id)

    def update_note(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("update", args, session_id=session_id)

    def append_note(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("append", args, session_id=session_id)

    def delete_note(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("delete", args, session_id=session_id)
