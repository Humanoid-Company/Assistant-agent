"""Glue between model tool calls and the local AgentRouter: typed args in, spoken/JSON replies out."""
from __future__ import annotations

import json

from agents.types import AgentResult

_CALENDAR_ARG_KEYS = (
    "action",
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


def _calendar_kwargs(args: dict) -> dict:
    """Typed calendar fields only. Identity and session never come from the model."""
    out = {
        key: args[key]
        for key in _CALENDAR_ARG_KEYS
        if key in args and args[key] is not None and key not in {"user_sub", "email", "session_id"}
    }
    if "action" in out:
        out["action"] = str(out["action"]).strip().lower()
    return out


def calendar_tool_args(
    args: dict,
    *,
    session_id: str | None = None,
    user_utterances: list[str] | None = None,
) -> dict:
    """Map a Realtime tool call onto CalendarAgent.

    session_id and user_utterances come from the server, never from the model.
    """
    out = _calendar_kwargs(args if isinstance(args, dict) else {})
    if session_id:
        out["session_id"] = session_id
    if user_utterances is not None:
        out["user_utterances"] = [item.strip() for item in user_utterances if isinstance(item, str) and item.strip()]
    return out


def _gmail_kwargs(args: dict) -> dict:
    keys = (
        "action",
        "to",
        "subject",
        "body",
        "query",
        "message_id",
        "draft_id",
        "confirmation",
        "op_id",
    )
    out = {k: args[k] for k in keys if k in args and args[k] is not None}
    if "action" in out:
        out["action"] = str(out["action"]).strip().lower()
    return out


def _notes_kwargs(args: dict) -> dict:
    keys = (
        "action",
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
    out = {k: args[k] for k in keys if k in args and args[k] is not None}
    if "action" in out:
        out["action"] = str(out["action"]).strip().lower()
    return out


def _run_connectivity_checks(router) -> AgentResult:
    """Local Google auth/API probe — no n8n / agent-ecosystem."""
    return router.check_connection()


def _describe_connection_status(result: AgentResult) -> str:
    """Spoken diagnosis from structured AgentResult (auth vs network vs OK)."""
    return result.message


def _model_tool_output(result: AgentResult) -> str:
    """JSON for the model. The spoken line stays in message; op_id is not for reading aloud."""
    data = result.data or {}
    body: dict = {"status": result.status, "message": result.message}
    for key in ("op_id", "reason_code", "missing_fields", "kind", "proposed_title"):
        value = data.get(key)
        if value not in (None, "", []):
            body[key] = value
    return json.dumps(body, ensure_ascii=False)


def _router_tool_failure_message(exc: BaseException) -> str:
    """Local argument errors are not a Google API failure."""
    if isinstance(exc, TypeError):
        return "Не вистачає параметрів команди. Повтори, що саме зробити."
    return "Не вдалося виконати запит до Google."


def _parse_router_reply(result: AgentResult) -> tuple[str, bool]:
    """Maps AgentResult → (spoken text, awaiting_user_reply) for deferred tool results."""
    reply = (result.message or "").strip() or "Роутер нічого не відповів."
    awaiting = result.awaiting_user_reply or result.status in (
        "needs_more_info",
        "confirmation_required",
        "auth_required",
        "permission_required",
    )
    return reply, awaiting


# Terminal / clarifying statuses that must be heard even if the model only calls note_emotion.
_SPEAK_ROUTER_STATUSES = frozenset(
    {
        "success",
        "confirmation_required",
        "needs_more_info",
        "ambiguous",
        "not_found",
        "error",
        "rate_limited",
        "permission_denied",
        "auth_required",
        "permission_required",
    }
)


def _should_speak_router_result(result: AgentResult) -> bool:
    return result.status in _SPEAK_ROUTER_STATUSES
