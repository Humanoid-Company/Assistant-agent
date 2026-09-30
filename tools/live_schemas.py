"""OpenAI Responses function schemas for the GPT-Live backend (no Gmail)."""
from __future__ import annotations

from robot_control import ROBOT_ACTIONS

# Keep in sync with assistant.VOICE_OPTIONS / LANGUAGE_OPTIONS without importing assistant
# (avoids audio/business import cycles in unit tests).
VOICE_OPTIONS: tuple[str, ...] = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)
LANGUAGE_OPTIONS: tuple[str, ...] = ("uk", "ru", "en")

_CALENDAR_PROPS = {
    "title": {"type": "string"},
    "date": {"type": "string", "description": "YYYY-MM-DD"},
    "time": {"type": "string", "description": "HH:MM local"},
    "duration_minutes": {"type": "integer"},
    "event_id": {"type": "string"},
    "calendar_id": {"type": "string"},
    "query": {"type": "string"},
    "new_date": {"type": "string"},
    "new_time": {"type": "string"},
    "new_start": {"type": "string", "description": "YYYY-MM-DDTHH:MM local"},
    "new_end": {"type": "string"},
    "new_summary": {"type": "string"},
    "new_description": {"type": "string"},
    "recurrence_scope": {"type": "string", "enum": ["instance", "series"]},
    "with_meet": {"type": "boolean"},
    "op_id": {"type": "string", "description": "From confirmation_required only"},
}


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    params: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": params,
    }


LIVE_BACKEND_TOOLS: list[dict] = [
    _fn(
        "calendar_list_events",
        "List upcoming Google Calendar events (read-only).",
        {"query": _CALENDAR_PROPS["query"], "date": _CALENDAR_PROPS["date"]},
    ),
    _fn(
        "calendar_search_events",
        "Search Google Calendar events by title/query (read-only).",
        {
            "query": _CALENDAR_PROPS["query"],
            "title": _CALENDAR_PROPS["title"],
            "date": _CALENDAR_PROPS["date"],
            "time": _CALENDAR_PROPS["time"],
        },
    ),
    _fn(
        "calendar_prepare_create",
        "Prepare creating a calendar event. Returns confirmation_required + op_id; does not create yet.",
        {
            "title": _CALENDAR_PROPS["title"],
            "date": _CALENDAR_PROPS["date"],
            "time": _CALENDAR_PROPS["time"],
            "duration_minutes": _CALENDAR_PROPS["duration_minutes"],
            "new_summary": _CALENDAR_PROPS["new_summary"],
            "new_start": _CALENDAR_PROPS["new_start"],
            "with_meet": _CALENDAR_PROPS["with_meet"],
            "new_description": _CALENDAR_PROPS["new_description"],
        },
    ),
    _fn(
        "calendar_prepare_update",
        "Prepare edit/reschedule. Returns confirmation_required + op_id.",
        {
            "title": _CALENDAR_PROPS["title"],
            "query": _CALENDAR_PROPS["query"],
            "date": _CALENDAR_PROPS["date"],
            "time": _CALENDAR_PROPS["time"],
            "event_id": _CALENDAR_PROPS["event_id"],
            "calendar_id": _CALENDAR_PROPS["calendar_id"],
            "new_date": _CALENDAR_PROPS["new_date"],
            "new_time": _CALENDAR_PROPS["new_time"],
            "new_start": _CALENDAR_PROPS["new_start"],
            "new_end": _CALENDAR_PROPS["new_end"],
            "new_summary": _CALENDAR_PROPS["new_summary"],
            "new_description": _CALENDAR_PROPS["new_description"],
            "duration_minutes": _CALENDAR_PROPS["duration_minutes"],
            "recurrence_scope": _CALENDAR_PROPS["recurrence_scope"],
            "action": {"type": "string", "enum": ["edit", "update", "reschedule"]},
        },
    ),
    _fn(
        "calendar_prepare_delete",
        "Prepare cancel/delete. Returns confirmation_required + op_id.",
        {
            "title": _CALENDAR_PROPS["title"],
            "query": _CALENDAR_PROPS["query"],
            "date": _CALENDAR_PROPS["date"],
            "time": _CALENDAR_PROPS["time"],
            "event_id": _CALENDAR_PROPS["event_id"],
            "calendar_id": _CALENDAR_PROPS["calendar_id"],
            "recurrence_scope": _CALENDAR_PROPS["recurrence_scope"],
            "action": {"type": "string", "enum": ["cancel", "delete"]},
        },
    ),
    _fn(
        "calendar_confirm_operation",
        "Confirm a pending calendar mutation using op_id from confirmation_required.",
        {"op_id": _CALENDAR_PROPS["op_id"]},
        required=["op_id"],
    ),
    _fn(
        "calendar_reject_operation",
        "Reject/cancel a pending calendar mutation using op_id.",
        {"op_id": _CALENDAR_PROPS["op_id"]},
        required=["op_id"],
    ),
    _fn(
        "google_account",
        "Browser OAuth Google account: connect, status, disconnect, reauth_switch, lock_session. "
        "grant_gmail exists for legacy accounts but Gmail tools are not available in Live mode.",
        {
            "action": {
                "type": "string",
                "enum": [
                    "connect",
                    "status",
                    "disconnect",
                    "grant_gmail",
                    "reauth_switch",
                    "lock_session",
                ],
            },
            "with_gmail": {"type": "boolean"},
        },
        required=["action"],
    ),
    _fn(
        "set_assistant_name",
        "Change the assistant's spoken name.",
        {"name": {"type": "string"}},
        required=["name"],
    ),
    _fn(
        "change_voice",
        "Change TTS voice. Live mode restarts the voice session (not the whole process).",
        {"voice": {"type": "string", "enum": list(VOICE_OPTIONS)}},
        required=["voice"],
    ),
    _fn(
        "change_language",
        "Change conversation language immediately.",
        {"language": {"type": "string", "enum": list(LANGUAGE_OPTIONS)}},
        required=["language"],
    ),
    _fn(
        "end_conversation",
        "End the conversation and return to sleep. Call silently.",
        {},
    ),
    _fn(
        "check_connection",
        "Check Google auth/API connectivity. Only on explicit user request.",
        {},
    ),
    _fn(
        "control_robot",
        "Physical robot action when the user explicitly requests motion/pose.",
        {"action": {"type": "string", "enum": list(ROBOT_ACTIONS)}},
        required=["action"],
    ),
]
