"""Thin calendar wrappers around existing CalendarAgent / AgentRouter."""
from __future__ import annotations

from typing import Any, Callable

from agents.types import AgentResult

# Keys accepted from the model for calendar wrappers (identity never from model).
_CALENDAR_FIELDS = (
    "title",
    "date",
    "time",
    "duration_minutes",
    "event_id",
    "calendar_id",
    "query",
    "new_date",
    "new_time",
    "new_start",
    "new_end",
    "new_summary",
    "new_description",
    "recurrence_scope",
    "with_meet",
    "confirmation",
    "op_id",
)


def calendar_fields(args: dict[str, Any]) -> dict[str, Any]:
    out = {
        key: args[key]
        for key in _CALENDAR_FIELDS
        if key in args and args[key] is not None and key not in {"user_sub", "email", "session_id"}
    }
    return out


class CalendarToolWrappers:
    """Model-facing calendar ops that delegate to AgentRouter.calendar_action."""

    def __init__(self, calendar_action: Callable[..., AgentResult]) -> None:
        self._calendar_action = calendar_action

    def _call(self, action: str, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        kwargs = calendar_fields(args)
        kwargs["action"] = action
        if session_id:
            kwargs["session_id"] = session_id
        utterances = args.get("user_utterances")
        if isinstance(utterances, list):
            kwargs["user_utterances"] = [
                item.strip() for item in utterances if isinstance(item, str) and item.strip()
            ]
        return self._calendar_action(**kwargs)

    def list_events(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("list", args, session_id=session_id)

    def search_events(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("search", args, session_id=session_id)

    def prepare_create(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("create", args, session_id=session_id)

    def prepare_update(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        # Prefer edit; reschedule aliases are accepted by CalendarAgent.
        action = str(args.get("action") or "edit").strip().lower()
        if action not in {"edit", "update", "reschedule"}:
            action = "edit"
        return self._call(action, args, session_id=session_id)

    def prepare_delete(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        action = str(args.get("action") or "cancel").strip().lower()
        if action not in {"cancel", "delete"}:
            action = "cancel"
        return self._call(action, args, session_id=session_id)

    def confirm(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        payload = dict(args)
        payload["confirmation"] = "yes"
        return self._call("confirm", payload, session_id=session_id)

    def reject(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        payload = dict(args)
        payload["confirmation"] = "no"
        return self._call("confirm", payload, session_id=session_id)
