"""Calendar tool-call validation and shared constants."""
from __future__ import annotations

import re

from agents.types import AgentResult
from integrations.google_calendar import (
    normalize_hhmm,
)

_PRIMARY = "primary"
_YES = {"yes", "так", "підтверджую", "да"}
_NO = {"no", "ні", "нет", "cancel", "reject", "скасуй", "не треба"}
_AMBIGUOUS_MSG = (
    "Не вдалося підтвердити результат у Google. Дія могла вже виконатись — "
    "перевір календар вручну і не повторюй «так»."
)
_CREATE_LABELS = {
    "title": "назва",
    "date": "дата (РРРР-ММ-ДД)",
    "time": "час (ГГ:ХХ)",
}
_CALENDAR_ACTIONS = frozenset(
    {
        "list",
        "view",
        "agenda",
        "search",
        "create",
        "edit",
        "update",
        "reschedule",
        "move",
        "cancel",
        "delete",
        "confirm",
        "reject",
    }
)
_CALENDAR_STRING_FIELDS = (
    "action",
    "title",
    "date",
    "time",
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
    "confirmation",
    "op_id",
    "session_id",
)


def calendar_call_problem(kwargs: dict) -> AgentResult | None:
    """Reject a tool call before any Google operation. None means the call may proceed."""
    if not isinstance(kwargs, dict):
        return AgentResult(
            "needs_more_info",
            "Не розібрав аргументи календаря. Скажи, що зробити: переглянути, створити, змінити чи скасувати.",
            {"missing_fields": ["action"], "invalid_fields": []},
        )
    invalid = [
        name
        for name in _CALENDAR_STRING_FIELDS
        if name in kwargs and kwargs[name] is not None and not isinstance(kwargs[name], str)
    ]
    if "duration_minutes" in kwargs and kwargs["duration_minutes"] is not None:
        minutes = kwargs["duration_minutes"]
        digit = isinstance(minutes, str) and minutes.strip().isdigit()
        whole = isinstance(minutes, int) and not isinstance(minutes, bool)
        if not digit and not whole:
            invalid.append("duration_minutes")
    if "with_meet" in kwargs and kwargs["with_meet"] is not None and not isinstance(kwargs["with_meet"], bool):
        invalid.append("with_meet")
    utterances = kwargs.get("user_utterances")
    if utterances is not None and (
        not isinstance(utterances, list) or any(not isinstance(item, str) for item in utterances)
    ):
        invalid.append("user_utterances")
    if invalid:
        names = ", ".join(invalid)
        return AgentResult(
            "needs_more_info",
            f"Некоректний тип параметрів: {names}. Повтори команду ще раз.",
            {"invalid_fields": invalid, "missing_fields": []},
        )
    raw_action = kwargs.get("action")
    action = raw_action.strip().lower() if isinstance(raw_action, str) else ""
    if not action:
        return AgentResult(
            "needs_more_info",
            "Команда календаря прийшла без дії. Скажи, що зробити: переглянути, створити, змінити чи скасувати.",
            {"missing_fields": ["action"], "invalid_fields": []},
        )
    if action not in _CALENDAR_ACTIONS:
        return AgentResult(
            "needs_more_info",
            "Такої дії календаря немає. Скажи: переглянути, створити, змінити чи скасувати.",
            {"invalid_fields": ["action"], "missing_fields": []},
        )
    return None


def _blank(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clock_value(value: str | None) -> str | None:
    text = _blank(value)
    if text is None:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return normalize_hhmm(text)
    return text


def _missing_create(fields: list[str]) -> AgentResult:
    labels = ", ".join(_CREATE_LABELS[name] for name in fields)
    return AgentResult(
        "needs_more_info",
        f"Щоб створити подію, бракує: {labels}.",
        {"missing_fields": fields},
    )
