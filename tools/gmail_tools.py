"""Thin Gmail wrappers around existing GmailAgent / AgentRouter."""
from __future__ import annotations

from typing import Any, Callable

from agents.types import AgentResult

# Keys accepted from the model (identity / session never from the model).
_GMAIL_FIELDS = (
    "to",
    "subject",
    "body",
    "query",
    "message_id",
    "draft_id",
    "confirmation",
    "op_id",
)


def gmail_fields(args: dict[str, Any]) -> dict[str, Any]:
    return {
        key: args[key]
        for key in _GMAIL_FIELDS
        if key in args and args[key] is not None and key not in {"user_sub", "email", "session_id"}
    }


class GmailToolWrappers:
    """Model-facing Gmail ops that delegate to AgentRouter.gmail_action / GmailAgent."""

    def __init__(self, gmail_action: Callable[..., AgentResult]) -> None:
        self._gmail_action = gmail_action

    def _call(self, action: str, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        kwargs = gmail_fields(args)
        kwargs["action"] = action
        if session_id:
            kwargs["session_id"] = session_id
        return self._gmail_action(**kwargs)

    def search_messages(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("search", args, session_id=session_id)

    def read_message(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("read", args, session_id=session_id)

    def create_draft(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("draft", args, session_id=session_id)

    def prepare_send(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("send", args, session_id=session_id)

    def prepare_reply(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        return self._call("reply", args, session_id=session_id)

    def confirm_send(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        payload = dict(args)
        payload["confirmation"] = "yes"
        return self._call("confirm", payload, session_id=session_id)

    def reject_send(self, args: dict[str, Any], *, session_id: str | None) -> AgentResult:
        payload = dict(args)
        payload["confirmation"] = "no"
        return self._call("confirm", payload, session_id=session_id)
