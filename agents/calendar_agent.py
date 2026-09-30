"""Calendar agent — view/search/create/edit/cancel with human confirmation."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from agents.create_draft import (
    CreateDraftStore,
    best_title_span,
    is_acknowledgement,
    is_draft_cancel,
    is_title_confirmation,
    title_said_by_user,
    title_similarity,
)
from agents.pending_store import PendingConflict, PendingStore
from agents.types import AgentResult, result_from_google_error
from auth.account_manager import AccountManager
from auth.google_oauth import OAuthError
from integrations.google_calendar import (
    CalendarClient,
    GoogleCalendarClient,
    extract_move_phrases,
    interpret_local_start,
    local_wall_time_problem,
    normalize_hhmm,
    parse_search_criteria,
    relative_or_iso_date,
    resolve_start_end,
    speak_clock,
    speak_date,
    speak_local_when,
    split_local_datetime,
    validate_date_time,
)
from integrations.google_errors import GoogleApiError

logger = logging.getLogger(__name__)

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
_CHOICE_TITLE_RE = re.compile(r"\b(або|чи|or)\b", re.IGNORECASE)
_HOUR_RE = re.compile(r"(?:о|в|у|на)\s+(\d{1,2})(?:[:.](\d{2}))?(?!\d)", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)")
_EVENING_RE = re.compile(
    r"(?:о|в|у|на)?\s*(\d{1,2})(?:[:.](\d{2}))?\s*(вечора|вечір|вечера|вечером|дня|днем|ранку|зранку|ночі|ночью)",
    re.IGNORECASE,
)
# Spoken hour words (uk/ru cardinals + common ordinals used in "на сьому годину").
_HOUR_WORDS: dict[str, int] = {
    "нуль": 0,
    "один": 1,
    "одна": 1,
    "першу": 1,
    "перша": 1,
    "первой": 1,
    "два": 2,
    "дві": 2,
    "другу": 2,
    "друга": 2,
    "второй": 2,
    "три": 3,
    "третю": 3,
    "третя": 3,
    "третью": 3,
    "чотири": 4,
    "четверту": 4,
    "четверта": 4,
    "четвертую": 4,
    "пять": 5,
    "п'ять": 5,
    "пят": 5,
    "пʼять": 5,
    "пʼяту": 5,
    "п'яту": 5,
    "пятую": 5,
    "шість": 6,
    "шесть": 6,
    "шосту": 6,
    "шоста": 6,
    "шестую": 6,
    "сім": 7,
    "семь": 7,
    "сьому": 7,
    "сьома": 7,
    "седьмую": 7,
    "седьмую": 7,
    "восемь": 8,
    "вісім": 8,
    "восьму": 8,
    "восьма": 8,
    "восьмую": 8,
    "дев'ять": 9,
    "девять": 9,
    "девʼять": 9,
    "дев'яту": 9,
    "девʼяту": 9,
    "девятую": 9,
    "десять": 10,
    "десяту": 10,
    "десятая": 10,
    "десятую": 10,
    "одинадцять": 11,
    "одиннадцать": 11,
    "одинадцяту": 11,
    "одиннадцатую": 11,
    "дванадцять": 12,
    "двенадцать": 12,
    "дванадцяту": 12,
    "двенадцатую": 12,
    "тринадцять": 13,
    "тринадцать": 13,
    "тринадцяту": 13,
    "тринадцатую": 13,
    "чотирнадцять": 14,
    "четырнадцать": 14,
    "чотирнадцяту": 14,
    "четырнадцатую": 14,
    "п'ятнадцять": 15,
    "пятнадцать": 15,
    "пʼятнадцять": 15,
    "п'ятнадцяту": 15,
    "пʼятнадцяту": 15,
    "пятнадцатую": 15,
    "шістнадцять": 16,
    "шестнадцать": 16,
    "шістнадцяту": 16,
    "шестнадцатую": 16,
    "сімнадцять": 17,
    "семнадцать": 17,
    "сімнадцяту": 17,
    "семнадцатую": 17,
    "вісімнадцять": 18,
    "восемнадцать": 18,
    "вісімнадцяту": 18,
    "восемнадцатую": 18,
    "дев'ятнадцять": 19,
    "девятнадцать": 19,
    "девʼятнадцять": 19,
    "дев'ятнадцяту": 19,
    "девʼятнадцяту": 19,
    "девятнадцатую": 19,
    "девятнадцата": 19,
    "девятнадцатый": 19,
    "двадцять": 20,
    "двадцать": 20,
    "двадцяту": 20,
    "двадцатую": 20,
    "двадцять одну": 21,
    "двадцать один": 21,
    "двадцять дві": 22,
    "двадцать два": 22,
    "двадцять три": 23,
    "двадцать три": 23,
}
_WORD_HOUR_RE = re.compile(
    r"(?:о|в|у|на|о\s*коло)?\s*"
    r"(нуль|один|одна|перш\w*|два|дві|друг\w*|три|трет\w*|чотири|четверт\w*|"
    r"п['ʼ]?ят\w*|пять|пят\w*|шість|шесть|шост\w*|шест\w*|сім|семь|сьом\w*|седьм\w*|"
    r"вісім|восемь|восьм\w*|дев['ʼ]?ят\w*|десят\w*|одинадцят\w*|одиннадцат\w*|"
    r"дванадцят\w*|двенадцат\w*|тринадцят\w*|тринадцат\w*|чотирнадцят\w*|четырнадцат\w*|"
    r"п['ʼ]?ятнадцят\w*|пятнадцат\w*|шістнадцят\w*|шестнадцат\w*|сімнадцят\w*|семнадцат\w*|"
    r"вісімнадцят\w*|восемнадцат\w*|дев['ʼ]?ятнадцят\w*|девятнадцат\w*|"
    r"двадцят\w*|двадцат\w*(?:\s+(?:один|одна|два|дві|три))?)"
    r"(?:\s+(?:нуль|ноль)\s+(?:нуль|ноль))?"
    r"(?:\s*(?:годин\w*|час\w*|часа))?"
    r"(?:\s*(вечора|вечір|вечера|вечером|дня|днем|ранку|зранку|ночі|ночью))?",
    re.IGNORECASE,
)
_SPOKEN_HHMM_RE = re.compile(
    r"\b(\d{1,2})\s+(?:нуль|ноль|00)\s+(?:нуль|ноль|00)\b",
    re.IGNORECASE,
)


def _hour_period(hour: int, period: str | None) -> int:
    if not period:
        return hour
    label = period.casefold()
    if "ранк" in label or "зранк" in label:
        return hour % 12
    if "веч" in label or "дня" in label or "днем" in label:
        return hour if 12 <= hour <= 23 else (hour + 12 if hour < 12 else hour)
    if "ноч" in label:
        return 0 if hour == 12 else hour % 12
    return hour


def _lookup_hour_word(token: str) -> int | None:
    key = (
        token.casefold()
        .replace("ʼ", "'")
        .replace("`", "'")
        .replace("’", "'")
        .strip()
    )
    key = re.sub(r"\s+", " ", key)
    if key in _HOUR_WORDS:
        return _HOUR_WORDS[key]
    stems = (
        ("девятнадцат", 19),
        ("дев'ятнадцят", 19),
        ("семнадцат", 17),
        ("сімнадцят", 17),
        ("восемнадцат", 18),
        ("вісімнадцят", 18),
        ("шестнадцат", 16),
        ("шістнадцят", 16),
        ("пятнадцат", 15),
        ("п'ятнадцят", 15),
        ("четырнадцат", 14),
        ("чотирнадцят", 14),
        ("тринадцат", 13),
        ("тринадцят", 13),
        ("двенадцат", 12),
        ("дванадцят", 12),
        ("одиннадцат", 11),
        ("одинадцят", 11),
        ("двадцать три", 23),
        ("двадцять три", 23),
        ("двадцать два", 22),
        ("двадцять дві", 22),
        ("двадцать один", 21),
        ("двадцять од", 21),
        ("двадцат", 20),
        ("двадцят", 20),
        ("десят", 10),
        ("дев'ят", 9),
        ("девять", 9),
        ("девятую", 9),
        ("восьм", 8),
        ("сьом", 7),
        ("седьм", 7),
        ("шост", 6),
        ("шест", 6),
        ("п'ят", 5),
        ("пят", 5),
        ("четверт", 4),
        ("трет", 3),
        ("друг", 2),
        ("перш", 1),
        ("перв", 1),
    )
    for stem, value in stems:
        if key.startswith(stem):
            return value
    return None


def _mentioned_times(text: str) -> set[str]:
    found: set[str] = set()
    for match in _CLOCK_RE.finditer(text):
        hour = int(match.group(1))
        if hour <= 23:
            found.add(f"{hour:02d}:{match.group(2)}")
    for match in _EVENING_RE.finditer(text):
        hour = _hour_period(int(match.group(1)), match.group(3))
        minute = match.group(2) or "00"
        if hour <= 23:
            found.add(f"{hour:02d}:{minute}")
    for match in _HOUR_RE.finditer(text):
        hour = int(match.group(1))
        minute = match.group(2) or "00"
        if hour <= 23:
            found.add(f"{hour:02d}:{minute}")
            if 1 <= hour <= 11:
                found.add(f"{hour + 12:02d}:{minute}")
    for match in _SPOKEN_HHMM_RE.finditer(text):
        hour = int(match.group(1))
        if hour <= 23:
            found.add(f"{hour:02d}:00")
    for match in _WORD_HOUR_RE.finditer(text):
        hour = _lookup_hour_word(match.group(1))
        if hour is None:
            continue
        period = match.group(2)
        hour = _hour_period(hour, period)
        if hour > 23:
            continue
        found.add(f"{hour:02d}:00")
        if period is None and 1 <= hour <= 11:
            found.add(f"{hour + 12:02d}:00")
    return found


def _is_acknowledgement(text: str) -> bool:
    return is_acknowledgement(text)


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


_DEICTIC_RE = re.compile(
    r"^(цю(\s+подію|\s+зустріч)?|ця(\s+подія)?|її|його|цю\s+саму|"
    r"(цю\s+)?подію,?\s+яку\s+щойно\s+знайшли|щойно\s+знайден\w*|"
    r"останн\w+(\s+подію|\s+зустріч)?)$",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"(\d+)\s*хвилин", re.IGNORECASE)


def _is_deictic(text: str | None) -> bool:
    if not text or not str(text).strip():
        return False
    cleaned = re.sub(
        r"\b(перенеси|перенести|зміни|змінити|скасуй|видали|видалити|подію|подія|зустріч)\b",
        " ",
        str(text).lower(),
    )
    cleaned = re.sub(r"[^\w\s'’ʼ-]+", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    if "щойно знайш" in cleaned:
        return True
    return _DEICTIC_RE.match(cleaned) is not None


def _uncertain(exc: GoogleApiError) -> bool:
    return exc.code in {"network", "timeout", "google_unavailable"} or (
        exc.http_status is not None and exc.http_status >= 500
    )


def _google_event_gone(event: dict | None) -> bool:
    """Google soft-deletes: events.get still returns the row with status=cancelled."""
    if not event:
        return True
    return str(event.get("status") or "").strip().lower() == "cancelled"


def _scope(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    token = str(value).strip().lower()
    if token in {"instance", "this", "цей", "лише цей"}:
        return "instance"
    if token in {"series", "all", "уся", "вся", "серія", "всю серію"}:
        return "series"
    return None


def _is_recurring(event: dict) -> bool:
    return bool(event.get("recurrence") or event.get("recurringEventId"))


def _mutation_id(event: dict, scope: str | None) -> str:
    if scope == "series":
        return str(event.get("recurringEventId") or event["id"])
    return str(event["id"])


def _snapshot(event: dict) -> dict:
    start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date") or ""
    end = (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date") or ""
    return {
        "summary": event.get("summary") or "",
        "description": event.get("description") or "",
        "start": start,
        "end": end,
        "etag": event.get("etag") or "",
    }


def _conflicts(saved: dict, live: dict) -> bool:
    current = _snapshot(live)
    for key in ("summary", "description", "start", "end"):
        if (saved.get(key) or "") != (current.get(key) or ""):
            return True
    if saved.get("etag") and current.get("etag") and saved["etag"] != current["etag"]:
        return True
    return False


def _patch_applied(fresh: dict, patch: dict) -> bool:
    if "summary" in patch and (fresh.get("summary") or "") != patch["summary"]:
        return False
    if "description" in patch and (fresh.get("description") or "") != patch["description"]:
        return False
    if "start" in patch:
        got = (fresh.get("start") or {}).get("dateTime")
        if got != (patch["start"] or {}).get("dateTime"):
            return False
    if "end" in patch:
        got = (fresh.get("end") or {}).get("dateTime")
        if got != (patch["end"] or {}).get("dateTime"):
            return False
    return True


def _duration_phrase(minutes: int) -> str:
    if minutes <= 0:
        return f"{minutes} хвилин"
    if minutes % 60 == 0:
        hours = minutes // 60
        if hours == 1:
            return "1 година"
        if 2 <= hours <= 4:
            return f"{hours} години"
        return f"{hours} годин"
    return f"{minutes} хвилин"


def _day_phrase(day: str, now: datetime) -> str:
    return speak_date(day, now=now)


def _parse_instant(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def _timed_bounds(event: dict) -> tuple[datetime, datetime] | None:
    start_raw = (event.get("start") or {}).get("dateTime")
    end_raw = (event.get("end") or {}).get("dateTime")
    if not start_raw or not end_raw:
        return None
    return _parse_instant(str(start_raw)), _parse_instant(str(end_raw))


def _local_parts(event: dict, timezone: str) -> tuple[str, str] | None:
    bounds = _timed_bounds(event)
    if bounds is None:
        return None
    local = bounds[0].astimezone(ZoneInfo(timezone))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _is_past(event: dict, now: datetime, timezone: str) -> bool:
    start_raw = (event.get("start") or {}).get("dateTime")
    if start_raw:
        return _parse_instant(str(start_raw)) < now
    day = (event.get("start") or {}).get("date")
    if day:
        return str(day) < now.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d")
    return False


class CalendarAgent:
    def __init__(
        self,
        accounts: AccountManager,
        pending: PendingStore,
        timezone: str = "Europe/Kyiv",
        client_factory: Callable[..., CalendarClient] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._accounts = accounts
        self._pending = pending
        self._timezone = timezone
        self._client_factory = client_factory or (lambda creds: GoogleCalendarClient(creds))
        self._now_source = clock
        self._selected: dict[tuple[str, str], dict] = {}
        self._drafts = CreateDraftStore()

    def clear_conversation_state(self, user_sub: str | None = None) -> None:
        """Drop unfinished create slots (account switch / disconnect / lock)."""
        if user_sub is None:
            self._drafts.clear_all()
        else:
            self._drafts.clear(user_sub)

    def _now(self) -> datetime:
        if self._now_source is None:
            return datetime.now(ZoneInfo(self._timezone))
        current = self._now_source()
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo(self._timezone))
        return current.astimezone(ZoneInfo(self._timezone))

    def _expand_date(self, value: str | None) -> str | None:
        text = _blank(value)
        if text is None:
            return None
        expanded = relative_or_iso_date(text, now=self._now(), timezone=self._timezone)
        return expanded or text

    def _client_for_user(self) -> tuple[str, CalendarClient]:
        sub, creds = self._accounts.credentials_for(calendar=True)
        return sub, self._client_factory(creds)

    def handle(
        self,
        action: str | None = None,
        *,
        title: str | None = None,
        date: str | None = None,
        time: str | None = None,
        duration_minutes: int | None = None,
        event_id: str | None = None,
        calendar_id: str | None = None,
        query: str | None = None,
        new_date: str | None = None,
        new_time: str | None = None,
        new_start: str | None = None,
        new_end: str | None = None,
        new_summary: str | None = None,
        new_description: str | None = None,
        recurrence_scope: str | None = None,
        with_meet: bool = False,
        confirmation: str | None = None,
        op_id: str | None = None,
        session_id: str | None = None,
        user_utterances: list[str] | None = None,
        user_sub: str | None = None,
        email: str | None = None,
        **_ignored: object,
    ) -> AgentResult:
        del user_sub, email  # never trust tool-supplied identity
        problem = calendar_call_problem(
            {
                "action": action,
                "title": title,
                "date": date,
                "time": time,
                "duration_minutes": duration_minutes,
                "event_id": event_id,
                "calendar_id": calendar_id,
                "query": query,
                "new_date": new_date,
                "new_time": new_time,
                "new_start": new_start,
                "new_end": new_end,
                "new_summary": new_summary,
                "new_description": new_description,
                "recurrence_scope": recurrence_scope,
                "with_meet": with_meet,
                "confirmation": confirmation,
                "op_id": op_id,
                "session_id": session_id,
                "user_utterances": user_utterances,
            }
        )
        if problem is not None:
            return problem
        action = (action or "").strip().lower()
        confirmation = (confirmation or "").strip().lower().strip(".!?") or None
        supplied = [
            name
            for name, value in (
                ("title", title),
                ("date", date),
                ("time", time),
                ("duration_minutes", duration_minutes),
                ("query", query),
                ("event_id", event_id),
                ("new_date", new_date),
                ("new_time", new_time),
                ("new_start", new_start),
                ("new_end", new_end),
                ("new_summary", new_summary),
                ("new_description", new_description),
                ("recurrence_scope", recurrence_scope),
                ("confirmation", confirmation),
                ("op_id", op_id),
            )
            if value not in (None, "")
        ]
        result = self._dispatch(
            action,
            title=title,
            date=date,
            time=time,
            duration_minutes=duration_minutes,
            event_id=event_id,
            calendar_id=calendar_id,
            query=query,
            new_date=new_date,
            new_time=new_time,
            new_start=new_start,
            new_end=new_end,
            new_summary=new_summary,
            new_description=new_description,
            recurrence_scope=recurrence_scope,
            with_meet=with_meet,
            confirmation=confirmation,
            op_id=op_id,
            session_id=session_id,
            user_utterances=user_utterances,
        )
        data = result.data or {}
        missing = data.get("missing_fields") or []
        logger.info(
            "calendar result action=%s status=%s kind=%s op_id=%s pending_state=%s event_id=%s google_error=%s reason=%s fields=%s missing_fields=%s",
            action,
            result.status,
            data.get("kind") or data.get("pending_kind"),
            data.get("op_id"),
            data.get("pending_state"),
            data.get("event_id"),
            data.get("google_error"),
            data.get("reason_code"),
            ",".join(supplied),
            ",".join(str(item) for item in missing),
        )
        return result

    def _dispatch(self, action: str, **kwargs) -> AgentResult:
        confirmation = kwargs["confirmation"]
        if action == "reject":
            return self._handle_confirmation("no", op_id=kwargs["op_id"], session_id=kwargs["session_id"])
        if action == "confirm":
            if confirmation not in _YES and confirmation not in _NO:
                return AgentResult(
                    "needs_more_info",
                    "Потрібне явне підтвердження: так або ні. Неясну фразу не вважаю згодою.",
                )
            return self._handle_confirmation(
                confirmation, op_id=kwargs["op_id"], session_id=kwargs["session_id"]
            )
        try:
            if action in ("list", "view", "agenda"):
                return self.list_upcoming(query=kwargs["query"], session_id=kwargs["session_id"])
            if action == "search":
                return self.search(
                    kwargs["query"] or kwargs["title"] or "",
                    date=kwargs["date"],
                    time=kwargs["time"],
                    session_id=kwargs["session_id"],
                )
            if action == "create":
                prepared = self._prepare_create(
                    title=kwargs["title"],
                    date=kwargs["date"],
                    time=kwargs["time"],
                    duration_minutes=kwargs["duration_minutes"],
                    new_date=kwargs["new_date"],
                    new_time=kwargs["new_time"],
                    new_start=kwargs["new_start"],
                    new_end=kwargs["new_end"],
                    new_summary=kwargs["new_summary"],
                    new_description=kwargs["new_description"],
                )
                if isinstance(prepared, AgentResult):
                    return prepared
                grounded = self._ground_create(
                    prepared,
                    kwargs.get("user_utterances"),
                    kwargs.get("session_id"),
                )
                if isinstance(grounded, AgentResult):
                    return grounded
                return self.propose_create(
                    **grounded,
                    with_meet=kwargs["with_meet"],
                    session_id=kwargs["session_id"],
                )
            if action in ("cancel", "delete"):
                return self.propose_cancel(
                    query=kwargs["query"],
                    title=kwargs["title"],
                    date=kwargs["date"],
                    time=kwargs["time"],
                    event_id=kwargs["event_id"],
                    calendar_id=kwargs["calendar_id"],
                    recurrence_scope=kwargs["recurrence_scope"],
                    session_id=kwargs["session_id"],
                )
            if action in ("reschedule", "move", "update", "edit"):
                return self.propose_edit(
                    query=kwargs["query"],
                    title=kwargs["title"],
                    date=kwargs["date"],
                    time=kwargs["time"],
                    event_id=kwargs["event_id"],
                    calendar_id=kwargs["calendar_id"],
                    new_date=kwargs["new_date"],
                    new_time=kwargs["new_time"],
                    new_start=kwargs["new_start"],
                    new_end=kwargs["new_end"],
                    new_summary=kwargs["new_summary"],
                    new_description=kwargs["new_description"],
                    duration_minutes=kwargs["duration_minutes"],
                    recurrence_scope=kwargs["recurrence_scope"],
                    session_id=kwargs["session_id"],
                )
            return AgentResult(
                "needs_more_info",
                "Яка дія з календарем: переглянути, створити, змінити чи скасувати?",
            )
        except PendingConflict:
            return self._busy_result()
        except (OAuthError, GoogleApiError) as exc:
            return result_from_google_error(exc)

    def _busy_result(self) -> AgentResult:
        try:
            sub = self._accounts.require_active_sub()
        except OAuthError as exc:
            return result_from_google_error(exc)
        op = self._pending.get(sub)
        if op is None:
            return AgentResult("error", "Попередня дія ще виконується. Зачекай результат.")
        status = "needs_more_info" if op.state == "pending" else "error"
        return AgentResult(
            status,
            f"Зараз очікую підтвердження: {op.summary_uk} Спочатку скажи «так» або «ні».",
            {"op_id": op.op_id, "pending_kind": op.kind, "pending_state": op.state},
        )

    def _guard_pending(self, sub: str) -> AgentResult | None:
        op = self._pending.get(sub)
        if op is None:
            return None
        if op.state == "pending":
            return AgentResult(
                "needs_more_info",
                f"Зараз очікую підтвердження: {op.summary_uk} Спочатку скажи «так» або «ні».",
                {"op_id": op.op_id, "pending_kind": op.kind, "pending_state": op.state},
            )
        if op.state == "executing":
            return AgentResult(
                "error",
                "Попередня дія ще виконується. Зачекай результат, не підтверджуй повторно.",
                {"op_id": op.op_id, "pending_state": op.state},
            )
        if op.state == "ambiguous":
            # Do not start a second destructive op while the previous outcome is unresolved.
            return AgentResult(
                "ambiguous",
                op.ambiguous_reason
                or (
                    "Попередня дія в календарі завершилась невизначено. "
                    "Не повторюю її. Перевір календар або скажи «так», щоб лише звірити стан."
                ),
                {
                    "op_id": op.op_id,
                    "pending_kind": op.kind,
                    "pending_state": "ambiguous",
                    "reason_code": "ambiguous_blocks_new_mutation",
                },
            )
        return None

    def _sel_key(self, sub: str, session_id: str | None) -> tuple[str, str]:
        return (sub, session_id or "")

    def _remember(self, sub: str, session_id: str | None, event: dict) -> None:
        self._selected[self._sel_key(sub, session_id)] = {
            "event_id": event.get("id"),
            "calendar_id": _PRIMARY,
        }

    def _forget(self, sub: str, session_id: str | None) -> None:
        self._selected.pop(self._sel_key(sub, session_id), None)

    def _forget_user(self, sub: str) -> None:
        for key in [key for key in self._selected if key[0] == sub]:
            self._selected.pop(key, None)

    def _recall(self, sub: str, session_id: str | None) -> dict | None:
        found = self._selected.get(self._sel_key(sub, session_id))
        if not found or found.get("calendar_id") != _PRIMARY:
            return None
        return found

    def list_upcoming(
        self,
        query: str | None = None,
        days: int = 7,
        session_id: str | None = None,
    ) -> AgentResult:
        sub, client = self._client_for_user()
        now = datetime.now(ZoneInfo(self._timezone))
        title, parsed_date, parsed_time = parse_search_criteria(query, now=now, timezone=self._timezone)
        events = self._collect(
            client,
            title=title,
            date=parsed_date,
            time=parsed_time,
            now=now,
            future_only=parsed_date is None,
            horizon_days=days,
        )
        return self._events_result(sub, session_id, events, search_mode=bool(query))

    def search(
        self,
        query: str,
        *,
        date: str | None = None,
        time: str | None = None,
        session_id: str | None = None,
    ) -> AgentResult:
        if not (query or "").strip() and not date and not time:
            return AgentResult("needs_more_info", "Що саме шукати в календарі?")
        sub, client = self._client_for_user()
        now = datetime.now(ZoneInfo(self._timezone))
        title, parsed_date, parsed_time = parse_search_criteria(query, now=now, timezone=self._timezone)
        events = self._collect(
            client,
            title=title,
            date=date or parsed_date,
            time=time or parsed_time,
            now=now,
            future_only=False,
            horizon_days=30,
        )
        return self._events_result(sub, session_id, events, search_mode=True)

    def _events_result(
        self,
        sub: str,
        session_id: str | None,
        events: list[dict],
        *,
        search_mode: bool,
    ) -> AgentResult:
        if not events:
            self._forget(sub, session_id)
            message = "Не знайшов такої події в календарі." if search_mode else "На найближчі дні подій не знайдено."
            status = "not_found" if search_mode else "success"
            return AgentResult(status, message, {"events": []})
        public = [self._public_event(event) for event in events[:8]]
        if len(events) == 1:
            self._remember(sub, session_id, events[0])
            line = self._spoken_line(events[0])
            if search_mode:
                message = (
                    f"Подію знайдено: {line}. "
                    "Це лише пошук. Щоб змінити або скасувати, скажи окремою командою."
                )
            else:
                message = "Ось найближчі події: " + line
            return AgentResult(
                "success",
                message,
                {"events": public, "selected_event_id": events[0].get("id")},
            )
        self._forget(sub, session_id)
        lines = [self._spoken_line(event) for event in events[:5]]
        if search_mode:
            message = (
                "Знайшов кілька подій: " + "; ".join(lines) + ". Уточни назву, дату й час. "
                "Нічого не видаляю і не змінюю."
            )
        else:
            message = "Ось найближчі події: " + "; ".join(lines)
        return AgentResult("success", message, {"events": public})

    def _prepare_create(
        self,
        *,
        title: str | None,
        date: str | None,
        time: str | None,
        duration_minutes: int | None,
        new_date: str | None,
        new_time: str | None,
        new_start: str | None,
        new_end: str | None,
        new_summary: str | None,
        new_description: str | None,
    ) -> AgentResult | dict:
        """Map create aliases onto title/date/time. Conflicts are not guessed."""
        canonical_title = _blank(title)
        alias_title = _blank(new_summary)
        if canonical_title and alias_title and canonical_title != alias_title:
            return AgentResult(
                "needs_more_info",
                "Конфлікт параметрів створення: title і new_summary задають різні назви. Уточни одну.",
                {"conflict_fields": ["title", "new_summary"], "missing_fields": []},
            )
        resolved_title = canonical_title or alias_title
        if resolved_title and _CHOICE_TITLE_RE.search(resolved_title):
            return AgentResult(
                "needs_more_info",
                "Уточни, як назвати подію. Я не обираю між варіантами сам.",
                {"missing_fields": ["title"], "invalid_fields": ["title"]},
            )

        resolved_date = self._expand_date(date)
        resolved_time = _clock_value(time)
        alias_date = self._expand_date(new_date)
        alias_time = _clock_value(new_time)
        if resolved_date and alias_date and resolved_date != alias_date:
            return AgentResult(
                "needs_more_info",
                "Конфлікт параметрів створення: date і new_date різні. Уточни одну дату.",
                {"conflict_fields": ["date", "new_date"], "missing_fields": []},
            )
        if resolved_time and alias_time and resolved_time != alias_time:
            return AgentResult(
                "needs_more_info",
                "Конфлікт параметрів створення: time і new_time різні. Уточни один час.",
                {"conflict_fields": ["time", "new_time"], "missing_fields": []},
            )
        resolved_date = resolved_date or alias_date
        resolved_time = resolved_time or alias_time

        if _blank(new_start):
            start_date, start_time, start_err = interpret_local_start(new_start, self._timezone)
            if start_err:
                return AgentResult("needs_more_info", start_err, {"invalid_fields": ["new_start"], "missing_fields": []})
            conflicts: list[str] = []
            if resolved_date and start_date and resolved_date != start_date:
                conflicts.extend(["date", "new_start"])
            if resolved_time and start_time and resolved_time != start_time:
                conflicts.extend(["time", "new_start"])
            if conflicts:
                return AgentResult(
                    "needs_more_info",
                    "Конфлікт параметрів створення: date/time і new_start вказують на різний момент. Уточни один початок.",
                    {"conflict_fields": conflicts, "missing_fields": []},
                )
            resolved_date = resolved_date or start_date
            resolved_time = resolved_time or start_time

        minutes = duration_minutes
        if _blank(new_end):
            if not resolved_date or not resolved_time:
                return AgentResult(
                    "needs_more_info",
                    "Щоб врахувати new_end, потрібні дата й час початку.",
                    {"missing_fields": [name for name, value in (("date", resolved_date), ("time", resolved_time)) if not value]},
                )
            end_date, end_time, end_err = interpret_local_start(new_end, self._timezone)
            if end_err:
                return AgentResult("needs_more_info", end_err, {"invalid_fields": ["new_end"], "missing_fields": []})
            end_problem = local_wall_time_problem(end_date, end_time, self._timezone)
            if end_problem:
                return AgentResult("needs_more_info", end_problem, {"invalid_fields": ["new_end"], "missing_fields": []})
            start_problem = local_wall_time_problem(resolved_date, resolved_time, self._timezone)
            if start_problem:
                return AgentResult("needs_more_info", start_problem, {"missing_fields": []})
            end_dt = datetime.fromisoformat(f"{end_date}T{end_time}:00").replace(tzinfo=ZoneInfo(self._timezone))
            begin = datetime.fromisoformat(f"{resolved_date}T{resolved_time}:00").replace(tzinfo=ZoneInfo(self._timezone))
            derived = int((end_dt - begin).total_seconds() // 60)
            if derived <= 0:
                return AgentResult(
                    "needs_more_info",
                    "Час завершення має бути пізніше за початок.",
                    {"conflict_fields": ["new_end"], "missing_fields": []},
                )
            if minutes is not None:
                try:
                    explicit_minutes = int(minutes)
                except (TypeError, ValueError):
                    return AgentResult(
                        "needs_more_info",
                        "Тривалість має бути кількістю хвилин.",
                        {"invalid_fields": ["duration_minutes"], "missing_fields": []},
                    )
                minutes = explicit_minutes
            if minutes is not None and minutes != derived:
                return AgentResult(
                    "needs_more_info",
                    "Конфлікт параметрів створення: duration_minutes і new_end дають різну тривалість. Уточни одну.",
                    {"conflict_fields": ["duration_minutes", "new_end"], "missing_fields": []},
                )
            minutes = derived

        description = _blank(new_description)
        return {
            "title": resolved_title,
            "date": resolved_date,
            "time": resolved_time,
            "duration_minutes": 60 if minutes is None else minutes,
            "description": description,
        }

    def _dates_in(self, text: str) -> set[str]:
        found: set[str] = set()
        folded = text.casefold()
        if "післязавтра" in folded:
            found.add((self._now() + timedelta(days=2)).strftime("%Y-%m-%d"))
            folded = folded.replace("післязавтра", " ")
        if "завтра" in folded:
            found.add((self._now() + timedelta(days=1)).strftime("%Y-%m-%d"))
        if "сьогодні" in folded:
            found.add(self._now().strftime("%Y-%m-%d"))
        for match in re.finditer(r"\d{4}-\d{2}-\d{2}", text):
            found.add(match.group(0))
        return found

    def _ground_create(
        self,
        prepared: dict,
        utterances: list[str] | None,
        session_id: str | None,
    ) -> AgentResult | dict:
        """Merge multi-turn slots; reject invented titles/schedules; keep date/time across title fixes."""
        if utterances is None:
            return prepared
        texts = [item.strip() for item in utterances if item and item.strip()]
        blob = " ".join(texts)
        latest = texts[-1] if texts else ""
        try:
            sub = self._accounts.require_active_sub()
        except OAuthError as exc:
            return result_from_google_error(exc)

        if is_draft_cancel(latest):
            self._drafts.clear(sub, session_id)
            return AgentResult(
                "success",
                "Добре, скасовую незавершене створення події.",
                {"reason_code": "create_draft_cancelled", "missing_fields": []},
            )

        draft = self._drafts.get_or_create(sub, session_id)
        draft.timezone = self._timezone

        # Persist schedule the user already said — even while title is still missing.
        spoken_dates = self._dates_in(blob)
        spoken_times = _mentioned_times(blob)
        prepared_date = prepared.get("date")
        prepared_time = prepared.get("time")
        prepared_title = (prepared.get("title") or "").strip() or None
        prepared_minutes = prepared.get("duration_minutes")

        if prepared_date and prepared_date in spoken_dates:
            draft.date = prepared_date
            draft.confirmed_fields.add("date")
        elif not draft.date and len(spoken_dates) == 1:
            draft.date = next(iter(spoken_dates))
            draft.confirmed_fields.add("date")

        if prepared_time and prepared_time in spoken_times:
            draft.time = prepared_time
            draft.confirmed_fields.add("time")
        elif not draft.time and spoken_times:
            # Prefer 12h+ evening interpretation when both 08:00 and 20:00 appear.
            evening = sorted(t for t in spoken_times if int(t.split(":")[0]) >= 12)
            draft.time = evening[0] if evening else sorted(spoken_times)[0]
            draft.confirmed_fields.add("time")

        if isinstance(prepared_minutes, int) and prepared_minutes > 0:
            # Duration only sticks when the user mentioned minutes, otherwise keep prior/default later.
            if re.search(r"\d+\s*хвилин", blob, re.IGNORECASE) or "duration_minutes" in draft.confirmed_fields:
                draft.duration_minutes = prepared_minutes
                draft.confirmed_fields.add("duration_minutes")

        # Title confirmation of a previously proposed normalization.
        title_just_confirmed = False
        if draft.proposed_title and is_title_confirmation(latest):
            draft.title = draft.proposed_title
            draft.proposed_title = None
            draft.confirmed_fields.add("title")
            prepared_title = draft.title
            title_just_confirmed = True

        if _is_acknowledgement(latest) and not prepared_title and not title_just_confirmed:
            self._drafts.save(draft)
            missing = draft.missing() or ["title"]
            return AgentResult(
                "needs_more_info",
                "Не почув назву події. Як її назвати?",
                {"missing_fields": missing, "reason_code": "title_not_from_user"},
            )

        if prepared_title:
            if title_just_confirmed or title_said_by_user(prepared_title, texts):
                draft.title = prepared_title if not title_just_confirmed else draft.title
                draft.proposed_title = None
                draft.confirmed_fields.add("title")
            elif draft.proposed_title and title_similarity(prepared_title, draft.proposed_title) >= 0.9:
                # Model repeated the pending proposal; still needs explicit yes unless user also said it.
                if is_title_confirmation(latest) or title_said_by_user(prepared_title, texts):
                    draft.title = prepared_title
                    draft.proposed_title = None
                    draft.confirmed_fields.add("title")
                else:
                    self._drafts.save(draft)
                    return AgentResult(
                        "needs_more_info",
                        f"Правильно зрозуміла назву як «{draft.proposed_title}»?",
                        {
                            "missing_fields": ["title"],
                            "reason_code": "title_needs_confirm",
                            "proposed_title": draft.proposed_title,
                        },
                    )
            else:
                # Soft normalization: close to what the user said → ask to confirm, do not invent.
                span = best_title_span(texts)
                if span and title_similarity(prepared_title, span) >= 0.72:
                    draft.proposed_title = prepared_title
                    self._drafts.save(draft)
                    return AgentResult(
                        "needs_more_info",
                        f"Правильно зрозуміла назву як «{prepared_title}»?",
                        {
                            "missing_fields": ["title"],
                            "reason_code": "title_needs_confirm",
                            "proposed_title": prepared_title,
                        },
                    )
                # Only promote a short clarifying line to title — never the original long request.
                short_clarify = (
                    span
                    and not _is_acknowledgement(span)
                    and not is_title_confirmation(span)
                    and len(span.split()) <= 8
                    and not re.search(
                        r"\b(завтра|сьогодні|післязавтра|запиши|додай|постав|створи|подію)\b",
                        span,
                        re.IGNORECASE,
                    )
                )
                if short_clarify:
                    draft.title = span
                    draft.proposed_title = None
                    draft.confirmed_fields.add("title")
                else:
                    self._drafts.save(draft)
                    return AgentResult(
                        "needs_more_info",
                        "Не почув назву події. Як її назвати?",
                        {"missing_fields": ["title"], "reason_code": "title_not_from_user"},
                    )
        elif not draft.title:
            span = best_title_span(texts)
            if span and title_said_by_user(span, [span]) and not _is_acknowledgement(span):
                # Only auto-accept a short clarifying line as title when create was already in progress.
                if draft.date or draft.time:
                    draft.title = span
                    draft.confirmed_fields.add("title")
            if not draft.title:
                self._drafts.save(draft)
                return AgentResult(
                    "needs_more_info",
                    "Не почув назву події. Як її назвати?",
                    {"missing_fields": ["title"], "reason_code": "title_not_from_user"},
                )

        # Merge schedule: draft wins unless the latest utterance explicitly changes it.
        latest_dates = self._dates_in(latest)
        latest_times = _mentioned_times(latest)
        date = prepared_date or draft.date
        clock = prepared_time or draft.time
        if draft.date and date != draft.date and date not in latest_dates:
            date = draft.date
        if draft.time and clock != draft.time and clock not in latest_times:
            clock = draft.time
        if date and (date in spoken_dates or date == draft.date):
            draft.date = date
            draft.confirmed_fields.add("date")
        if clock and (clock in spoken_times or clock == draft.time):
            draft.time = clock
            draft.confirmed_fields.add("time")
        date = draft.date or date
        clock = draft.time or clock

        date_ok = bool(date) and (
            date in spoken_dates or ("date" in draft.confirmed_fields and date == draft.date)
        )
        time_ok = bool(clock) and (
            clock in spoken_times or ("time" in draft.confirmed_fields and clock == draft.time)
        )
        if not date_ok or not time_ok:
            missing = []
            if not date_ok:
                missing.append("date")
            if not time_ok:
                missing.append("time")
            self._drafts.save(draft)
            if "date" in missing and "time" not in missing:
                ask = "На яку дату поставити подію?"
            elif "time" in missing and "date" not in missing:
                ask = "На який час поставити подію?"
            else:
                ask = "Дата й час мають прозвучати від користувача. Повтори, на коли ставити подію."
            return AgentResult(
                "needs_more_info",
                ask,
                {"missing_fields": missing, "reason_code": "schedule_not_from_user"},
            )

        minutes = draft.duration_minutes if draft.duration_minutes is not None else prepared.get("duration_minutes", 60)
        title = draft.title or prepared_title
        self._drafts.clear(sub, session_id)
        return {
            **prepared,
            "title": title,
            "date": date,
            "time": clock,
            "duration_minutes": minutes,
        }

    def propose_create(
        self,
        *,
        title: str | None,
        date: str | None,
        time: str | None,
        duration_minutes: int = 60,
        with_meet: bool = False,
        session_id: str | None = None,
        description: str | None = None,
    ) -> AgentResult:
        sub = self._accounts.require_active_sub()
        busy = self._guard_pending(sub)
        if busy:
            return busy
        missing = [
            name
            for name, value in (("title", _blank(title)), ("date", _blank(date)), ("time", _blank(time)))
            if not value
        ]
        if missing:
            return _missing_create(missing)
        try:
            minutes = int(duration_minutes)
        except (TypeError, ValueError):
            return AgentResult("needs_more_info", "Тривалість має бути кількістю хвилин.", {"invalid_fields": ["duration_minutes"]})
        if minutes <= 0 or minutes > 24 * 60:
            return AgentResult(
                "needs_more_info",
                "Тривалість має бути від 1 до 1440 хвилин.",
                {"invalid_fields": ["duration_minutes"]},
            )
        err = local_wall_time_problem(date, time, self._timezone)
        if err:
            return AgentResult("needs_more_info", err, {"missing_fields": []})
        try:
            start, end = resolve_start_end(date, time, minutes, self._timezone)  # type: ignore[arg-type]
        except ValueError:
            return AgentResult("needs_more_info", "Некоректна дата або час.", {"missing_fields": []})
        if end <= start:
            return AgentResult("needs_more_info", "Час завершення має бути пізніше за початок.", {"missing_fields": []})
        if start < self._now():
            return AgentResult(
                "needs_more_info",
                "Ця дата й час уже минули. На яку дату й час поставити насправді?",
                {"missing_fields": []},
            )

        idempotency_key = str(uuid.uuid4())
        when = f"{speak_date(date, now=self._now())} о {speak_clock(time)}"
        summary = (
            f"Створити подію «{title}» на {when}"
            f"{' з Google Meet' if with_meet else ''}. Підтвердити?"
        )
        op = self._pending.put(
            sub,
            "calendar_create",
            summary,
            {
                "title": title.strip(),
                "date": date,
                "time": time,
                "duration_minutes": minutes,
                "with_meet": with_meet,
                "timezone": self._timezone,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "idempotency_key": idempotency_key,
                "calendar_id": _PRIMARY,
                "description": _blank(description),
            },
            session_id=session_id,
        )
        return AgentResult(
            "confirmation_required",
            summary,
            {"op_id": op.op_id, "kind": op.kind},
        )

    def propose_cancel(
        self,
        *,
        query: str | None,
        title: str | None,
        date: str | None,
        time: str | None,
        event_id: str | None,
        calendar_id: str | None,
        recurrence_scope: str | None,
        session_id: str | None,
    ) -> AgentResult:
        sub = self._accounts.require_active_sub()
        busy = self._guard_pending(sub)
        if busy:
            return busy
        now = datetime.now(ZoneInfo(self._timezone))
        resolved = self._resolve_event(
            sub=sub,
            session_id=session_id,
            query=query,
            title=title,
            date=date,
            time=time,
            event_id=event_id,
            calendar_id=calendar_id,
            now=now,
            ignore_dates=(),
            ignore_times=(),
        )
        if isinstance(resolved, AgentResult):
            return resolved
        event, explicit = resolved
        past = self._past_block(event, now, explicit=explicit or bool(event_id))
        if past:
            return past
        scope = _scope(recurrence_scope)
        if _is_recurring(event) and scope is None:
            return AgentResult(
                "needs_more_info",
                f"Це повторювана подія «{event.get('summary') or '(без назви)'}». "
                "Скасувати лише цей екземпляр чи всю серію? Нічого не видаляю.",
                {"event_id": event.get("id"), "recurring": True},
            )
        target_id = _mutation_id(event, scope)
        start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date", "")
        when = (
            speak_local_when(str(start), self._timezone, now=self._now())
            if "T" in str(start)
            else speak_date(str(start), now=self._now())
        )
        summary = f"Скасувати «{event.get('summary') or '(без назви)'}» ({when}). Підтверджуєш?"
        op = self._pending.put(
            sub,
            "calendar_cancel",
            summary,
            {
                "event_id": target_id,
                "calendar_id": _PRIMARY,
                "summary": event.get("summary"),
                "original": _snapshot(event if target_id == event.get("id") else {**event, "id": target_id}),
                "original_start": start,
                "timezone": self._timezone,
                "recurrence_scope": scope,
            },
            session_id=session_id,
        )
        # Snapshot must describe the event we will delete. Refetch series master when needed.
        if target_id != event.get("id"):
            _sub, client = self._client_for_user()
            try:
                master = client.get_event(target_id, calendar_id=_PRIMARY)
            except GoogleApiError:
                master = event
            op.payload["original"] = _snapshot(master)
            op.payload["summary"] = master.get("summary") or event.get("summary")
        return AgentResult(
            "confirmation_required",
            summary,
            {"op_id": op.op_id, "event_id": target_id, "kind": "calendar_cancel", "calendar_id": _PRIMARY},
        )

    def propose_edit(
        self,
        *,
        query: str | None,
        title: str | None,
        date: str | None,
        time: str | None,
        event_id: str | None,
        calendar_id: str | None,
        new_date: str | None,
        new_time: str | None,
        new_start: str | None,
        new_end: str | None,
        new_summary: str | None,
        new_description: str | None,
        duration_minutes: int | None,
        recurrence_scope: str | None,
        session_id: str | None,
    ) -> AgentResult:
        sub = self._accounts.require_active_sub()
        busy = self._guard_pending(sub)
        if busy:
            return busy
        now = datetime.now(ZoneInfo(self._timezone))
        text = " ".join(part for part in (query, title) if part)
        text, phrase_old_time, phrase_new_time, dest_token, dest_clock = extract_move_phrases(text)
        dest_date = (new_date or "").strip() or None
        dest_time = (new_time or "").strip() or None
        if new_start:
            parsed_date, parsed_time = split_local_datetime(new_start)
            dest_date = dest_date or parsed_date
            dest_time = dest_time or parsed_time
        if dest_token and not dest_date:
            dest_date = relative_or_iso_date(dest_token, now=now, timezone=self._timezone)
        if dest_clock and not dest_time:
            dest_time = dest_clock
        if phrase_new_time and not dest_time:
            dest_time = phrase_new_time
        if new_summary is None and duration_minutes is None and "тривал" in text.lower():
            duration_match = _DURATION_RE.search(text)
            if duration_match:
                duration_minutes = int(duration_match.group(1))

        early = self._validate_destination(dest_date, dest_time, new_end, duration_minutes)
        if early:
            return early

        explicit_new = any(
            (
                dest_date,
                dest_time,
                new_end,
                (new_summary or "").strip(),
                new_description is not None and str(new_description).strip(),
                duration_minutes is not None,
            )
        )
        search_date = date
        search_time = time or phrase_old_time
        if not explicit_new:
            # Legacy reschedule: date/time arguments are the new schedule, not search filters.
            dest_date = dest_date or date
            dest_time = dest_time or time
            search_date = None
            search_time = None
            early = self._validate_destination(dest_date, dest_time, new_end, duration_minutes)
            if early:
                return early

        ignore_dates = tuple(item for item in (new_date, dest_date if explicit_new else None) if item)
        ignore_times = tuple(
            item
            for item in (
                new_time,
                dest_time if explicit_new else None,
                *([split_local_datetime(new_end)[1]] if new_end else []),
            )
            if item
        )
        # A new date must not become the search date. Drop it from ignore when it is also
        # the only structured old date the caller passed separately via `date`.
        if search_date and search_date in ignore_dates and search_date == date:
            ignore_dates = tuple(item for item in ignore_dates if item != date)

        resolved = self._resolve_event(
            sub=sub,
            session_id=session_id,
            query=text,
            title=None if explicit_new else None,
            date=search_date,
            time=search_time,
            event_id=event_id,
            calendar_id=calendar_id,
            now=now,
            ignore_dates=ignore_dates,
            ignore_times=ignore_times,
            allow_empty_as_selection=True,
        )
        if isinstance(resolved, AgentResult):
            return resolved
        event, explicit = resolved
        past = self._past_block(event, now, explicit=explicit or bool(event_id) or bool(search_date))
        if past:
            return past
        scope = _scope(recurrence_scope)
        if _is_recurring(event) and scope is None:
            return AgentResult(
                "needs_more_info",
                f"Це повторювана подія «{event.get('summary') or '(без назви)'}». "
                "Змінити лише цей екземпляр чи всю серію? Нічого не змінюю.",
                {"event_id": event.get("id"), "recurring": True},
            )
        if dest_time and not dest_date:
            dest_date = search_date or (_local_parts(event, self._timezone) or (None, None))[0]
        planned = self._plan_edit(
            event,
            dest_date=dest_date,
            dest_time=dest_time,
            new_end=new_end,
            duration_minutes=duration_minutes,
            new_summary=(new_summary or "").strip() or None,
            new_description=new_description.strip() if isinstance(new_description, str) else new_description,
            now=now,
        )
        if isinstance(planned, AgentResult):
            return planned
        target_id = _mutation_id(event, scope)
        original_event = event
        if target_id != event.get("id"):
            _sub, client = self._client_for_user()
            try:
                original_event = client.get_event(target_id, calendar_id=_PRIMARY)
            except GoogleApiError:
                original_event = event
        op = self._pending.put(
            sub,
            planned["kind"],
            planned["summary_uk"],
            {
                "event_id": target_id,
                "calendar_id": _PRIMARY,
                "summary": original_event.get("summary") or event.get("summary"),
                "original": _snapshot(original_event),
                "patch": planned["patch"],
                "timezone": self._timezone,
                "success_message": planned["success_message"],
                "recurrence_scope": scope,
            },
            session_id=session_id,
        )
        return AgentResult(
            "confirmation_required",
            planned["summary_uk"],
            {
                "op_id": op.op_id,
                "event_id": target_id,
                "kind": planned["kind"],
                "calendar_id": _PRIMARY,
            },
        )

    def _validate_destination(
        self,
        dest_date: str | None,
        dest_time: str | None,
        new_end: str | None,
        duration_minutes: int | None,
    ) -> AgentResult | None:
        if dest_date:
            try:
                datetime.fromisoformat(f"{dest_date}T00:00:00")
            except ValueError:
                return AgentResult("needs_more_info", f"Некоректна дата «{dest_date}». Перевір день і місяць.")
        if dest_time:
            probe = validate_date_time("2000-01-01", dest_time)
            if probe:
                return AgentResult("needs_more_info", probe)
        if dest_date and dest_time:
            problem = local_wall_time_problem(dest_date, dest_time, self._timezone)
            if problem:
                return AgentResult("needs_more_info", problem)
        if new_end:
            end_date, end_time = split_local_datetime(new_end)
            problem = local_wall_time_problem(end_date, end_time, self._timezone)
            if problem:
                return AgentResult("needs_more_info", problem)
        if duration_minutes is not None:
            try:
                minutes = int(duration_minutes)
            except (TypeError, ValueError):
                return AgentResult("needs_more_info", "Тривалість має бути кількістю хвилин.")
            if minutes <= 0 or minutes > 24 * 60:
                return AgentResult("needs_more_info", "Тривалість має бути від 1 до 1440 хвилин.")
        return None

    def _plan_edit(
        self,
        event: dict,
        *,
        dest_date: str | None,
        dest_time: str | None,
        new_end: str | None,
        duration_minutes: int | None,
        new_summary: str | None,
        new_description: str | None,
        now: datetime,
    ) -> AgentResult | dict:
        bounds = _timed_bounds(event)
        wants_time = any((dest_date, dest_time, new_end, duration_minutes is not None))
        if wants_time and bounds is None:
            return AgentResult(
                "needs_more_info",
                "Ця подія без часу початку. Назви, що змінити в назві чи описі, або обери подію з годинами.",
            )
        patch: dict = {}
        start_dt = end_dt = None
        if bounds is not None:
            orig_start, orig_end = bounds
            local_start = orig_start.astimezone(ZoneInfo(self._timezone))
            start_dt = orig_start
            end_dt = orig_end
            if dest_date or dest_time:
                wall_date = dest_date or local_start.strftime("%Y-%m-%d")
                wall_time = dest_time or local_start.strftime("%H:%M")
                problem = local_wall_time_problem(wall_date, wall_time, self._timezone)
                if problem:
                    return AgentResult("needs_more_info", problem)
                start_dt = datetime.fromisoformat(f"{wall_date}T{wall_time}:00").replace(
                    tzinfo=ZoneInfo(self._timezone)
                )
            if new_end:
                end_date, end_time = split_local_datetime(new_end)
                problem = local_wall_time_problem(end_date, end_time, self._timezone)
                if problem:
                    return AgentResult("needs_more_info", problem)
                end_dt = datetime.fromisoformat(f"{end_date}T{end_time}:00").replace(tzinfo=ZoneInfo(self._timezone))
            elif duration_minutes is not None:
                end_dt = start_dt + timedelta(minutes=int(duration_minutes))
            elif start_dt != orig_start:
                end_dt = start_dt + (orig_end - orig_start)
            if end_dt <= start_dt:
                return AgentResult("needs_more_info", "Час завершення має бути пізніше за початок.")
            if start_dt != orig_start:
                patch["start"] = {"dateTime": start_dt.isoformat(), "timeZone": self._timezone}
                patch["end"] = {"dateTime": end_dt.isoformat(), "timeZone": self._timezone}
            elif end_dt != orig_end:
                patch["end"] = {"dateTime": end_dt.isoformat(), "timeZone": self._timezone}

        current_summary = event.get("summary") or ""
        if new_summary and new_summary != current_summary:
            patch["summary"] = new_summary
        if new_description is not None and str(new_description) != (event.get("description") or ""):
            if not str(new_description).strip():
                return AgentResult("needs_more_info", "Який опис додати до події?")
            patch["description"] = str(new_description).strip()
        if not patch:
            return AgentResult("needs_more_info", "Що саме змінити: час, назву, тривалість чи опис?")

        name = current_summary or "(без назви)"
        spoken: list[str] = []
        done: list[str] = []
        if "start" in patch and start_dt is not None and end_dt is not None and bounds is not None:
            old_hm = bounds[0].astimezone(ZoneInfo(self._timezone)).strftime("%H:%M")
            new_hm = start_dt.astimezone(ZoneInfo(self._timezone)).strftime("%H:%M")
            day = _day_phrase(start_dt.astimezone(ZoneInfo(self._timezone)).strftime("%Y-%m-%d"), now)
            minutes = int((end_dt - start_dt).total_seconds() // 60)
            spoken.append(
                f"Перенести «{name}» із {speak_clock(old_hm)} на {speak_clock(new_hm)} {day}, "
                f"тривалість {_duration_phrase(minutes)}"
            )
            done.append(
                f"Переніс «{name}» на "
                f"{speak_date(start_dt.astimezone(ZoneInfo(self._timezone)).strftime('%Y-%m-%d'), now=now)} "
                f"о {speak_clock(new_hm)}."
            )
        elif "end" in patch and end_dt is not None and start_dt is not None:
            minutes = int((end_dt - start_dt).total_seconds() // 60)
            spoken.append(f"Змінити тривалість «{name}» на {_duration_phrase(minutes)}")
            done.append(f"Змінив тривалість «{name}» на {_duration_phrase(minutes)}.")
        if "summary" in patch:
            spoken.append(f"Змінити назву «{name}» на «{patch['summary']}»")
            done.append(f"Змінив назву на «{patch['summary']}».")
        if "description" in patch:
            spoken.append(f"Додати опис до «{patch.get('summary') or name}»: {patch['description']}")
            done.append(f"Оновив опис події «{patch.get('summary') or name}».")
        kind = "calendar_reschedule" if "start" in patch else "calendar_update"
        return {
            "patch": patch,
            "kind": kind,
            "summary_uk": ". ".join(spoken) + ". Підтверджуєш?",
            "success_message": " ".join(done),
        }

    def _resolve_event(
        self,
        *,
        sub: str,
        session_id: str | None,
        query: str | None,
        title: str | None,
        date: str | None,
        time: str | None,
        event_id: str | None,
        calendar_id: str | None,
        now: datetime,
        ignore_dates: tuple[str, ...],
        ignore_times: tuple[str, ...],
        allow_empty_as_selection: bool = True,
    ) -> AgentResult | tuple[dict, bool]:
        rejected = self._reject_calendar_id(calendar_id)
        if rejected:
            return rejected
        _sub, client = self._client_for_user()
        if event_id:
            try:
                event = client.get_event(event_id, calendar_id=_PRIMARY)
            except GoogleApiError as exc:
                return result_from_google_error(exc)
            self._remember(sub, session_id, event)
            return event, True

        deictic = _is_deictic(query) or _is_deictic(title)
        blob = " ".join(part for part in (query, title) if part and not _is_deictic(part))
        parsed_title, parsed_date, parsed_time = parse_search_criteria(
            blob,
            now=now,
            timezone=self._timezone,
            ignore_dates=ignore_dates,
            ignore_times=ignore_times,
        )
        search_date = date or parsed_date
        search_time = time or parsed_time
        if search_date:
            try:
                datetime.fromisoformat(f"{search_date}T00:00:00")
            except ValueError:
                return AgentResult("needs_more_info", f"Некоректна дата «{search_date}». Перевір день і місяць.")
        if search_time:
            probe = validate_date_time("2000-01-01", search_time)
            if probe:
                return AgentResult("needs_more_info", probe)

        bare = not event_id and not parsed_title and not search_date and not search_time
        if deictic or (allow_empty_as_selection and bare):
            selected = self._recall(sub, session_id)
            if not selected:
                if deictic:
                    return AgentResult(
                        "needs_more_info",
                        "Не бачу вибраної події в цій сесії. Спочатку знайди її.",
                    )
                return AgentResult("needs_more_info", "Яку подію? Назви назву або час.")
            try:
                event = client.get_event(selected["event_id"], calendar_id=_PRIMARY)
            except GoogleApiError as exc:
                self._forget(sub, session_id)
                return result_from_google_error(exc)
            return event, True

        events = self._collect(
            client,
            title=parsed_title,
            date=search_date,
            time=search_time,
            now=now,
            future_only=False,
            horizon_days=30,
        )
        if not events:
            self._forget(sub, session_id)
            return AgentResult("not_found", "Не знайшов такої події в календарі.")
        if len(events) > 1:
            self._forget(sub, session_id)
            lines = [self._spoken_line(event) for event in events[:5]]
            return AgentResult(
                "needs_more_info",
                "Я бачу кілька подій: " + "; ".join(lines) + ". Уточни, яку саме?",
                {"candidates": [self._public_event(event) for event in events[:5]]},
            )
        self._remember(sub, session_id, events[0])
        return events[0], bool(search_date)

    def _collect(
        self,
        client: CalendarClient,
        *,
        title: str,
        date: str | None,
        time: str | None,
        now: datetime,
        future_only: bool,
        horizon_days: int,
    ) -> list[dict]:
        zone = ZoneInfo(self._timezone)
        if date:
            day = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=zone)
            time_min = day
            time_max = day + timedelta(days=1)
        elif future_only:
            time_min = now
            time_max = now + timedelta(days=horizon_days)
        else:
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
            time_max = now + timedelta(days=horizon_days)
        events = client.list_events(time_min, time_max, query=title or None)
        if title:
            tokens = [part.casefold() for part in title.split() if len(part) > 1]
            events = [
                event
                for event in events
                if all(token in (event.get("summary") or "").casefold() for token in tokens)
            ]
        if time:
            events = [event for event in events if self._clock(event) == time]
        if date:
            events = [event for event in events if self._day(event) == date]
        return events

    def _clock(self, event: dict) -> str | None:
        parts = _local_parts(event, self._timezone)
        if parts:
            return parts[1]
        return None

    def _day(self, event: dict) -> str | None:
        parts = _local_parts(event, self._timezone)
        if parts:
            return parts[0]
        day = (event.get("start") or {}).get("date")
        return str(day) if day else None

    def _past_block(self, event: dict, now: datetime, *, explicit: bool) -> AgentResult | None:
        if explicit or not _is_past(event, now, self._timezone):
            return None
        when = self._spoken_line(event)
        return AgentResult(
            "needs_more_info",
            f"Подія {when} уже минула. Назви дату явно, якщо треба змінити або скасувати саме її.",
            {"event_id": event.get("id")},
        )

    def _reject_calendar_id(self, calendar_id: str | None) -> AgentResult | None:
        if calendar_id and calendar_id != _PRIMARY:
            return AgentResult(
                "error",
                "Не використовую calendar_id із запиту. Працюю лише з основним календарем активного акаунта.",
                {"google_error": "forbidden_calendar"},
            )
        return None

    def _public_event(self, event: dict) -> dict:
        start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date") or ""
        end = (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date") or ""
        timezone = (event.get("start") or {}).get("timeZone") or self._timezone
        return {
            "event_id": event.get("id"),
            "calendar_id": _PRIMARY,
            "summary": event.get("summary") or "(без назви)",
            "start": start,
            "end": end,
            "timezone": timezone,
            "recurring": _is_recurring(event),
            "recurring_event_id": event.get("recurringEventId"),
        }

    def _spoken_line(self, event: dict) -> str:
        start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date") or ""
        when = (
            speak_local_when(str(start), self._timezone, now=self._now())
            if "T" in str(start)
            else speak_date(str(start), now=self._now())
        )
        return f"{when} — {event.get('summary') or '(без назви)'}"

    def _handle_confirmation(
        self,
        confirmation: str,
        *,
        op_id: str | None,
        session_id: str | None,
    ) -> AgentResult:
        try:
            sub = self._accounts.require_active_sub()
        except OAuthError as exc:
            return result_from_google_error(exc)

        # Confirm ends the create slot-filling conversation regardless of yes/no.
        self._drafts.clear(sub, session_id)

        yes = confirmation in _YES
        no = confirmation in _NO
        if not yes and not no:
            return AgentResult(
                "needs_more_info",
                "Потрібна чітка відповідь «так» або «ні». Неясну фразу не вважаю згодою.",
            )

        op, pending_reason = self._pending.get_with_reason(sub)
        if op is None:
            if pending_reason == "pending_expired":
                return self._confirm_error(
                    "pending_expired",
                    "Час підтвердження вичерпано. Скажи дію ще раз — нічого не змінюю.",
                )
            return self._confirm_error(
                "pending_missing",
                "Немає незавершеної дії для підтвердження.",
            )
        session_match = not (session_id and op.session_id and session_id != op.session_id)
        op_match = not op_id or op_id == op.op_id
        if op.user_sub != sub:
            return self._confirm_error(
                "account_mismatch",
                "Підтвердження не відповідає активному акаунту.",
                op_match=op_match,
                session_match=session_match,
            )
        if not session_match:
            return self._confirm_error(
                "session_mismatch",
                "Підтвердження належить іншій сесії.",
                op_id=op.op_id,
                op_match=op_match,
                session_match=False,
                pending_state=op.state,
            )
        if not op_match:
            return self._confirm_error(
                "op_id_mismatch",
                "Це підтвердження не відповідає очікуваній дії. Скажи ще раз так або ні.",
                op_id=op.op_id,
                op_match=False,
                session_match=True,
                pending_state=op.state,
            )
        if op.kind not in ("calendar_create", "calendar_cancel", "calendar_reschedule", "calendar_update"):
            return self._confirm_error(
                "wrong_kind",
                "Це підтвердження не для календарної дії.",
                op_id=op.op_id,
                pending_state=op.state,
            )
        if op.state == "completed" and op.execution_result:
            return AgentResult(
                "success",
                op.execution_result.get("message", "Вже виконано раніше."),
                op.execution_result,
            )
        if op.state == "ambiguous":
            if yes and op.kind == "calendar_cancel":
                # Re-check the exact event — never delete again.
                try:
                    _sub, client = self._client_for_user()
                except (OAuthError, GoogleApiError) as exc:
                    return result_from_google_error(exc)
                if _sub != sub:
                    return self._confirm_error(
                        "account_mismatch",
                        "Підтвердження не відповідає активному акаунту.",
                        op_id=op.op_id,
                        pending_state="ambiguous",
                    )
                return self._reconcile_cancel(sub, client, op.payload, op_id=op.op_id)
            return AgentResult(
                "ambiguous",
                op.ambiguous_reason or _AMBIGUOUS_MSG,
                {"op_id": op.op_id, "pending_state": "ambiguous", "kind": op.kind},
            )
        if no:
            if op.state == "executing" or not self._pending.mark_cancelled(sub):
                return self._confirm_error(
                    "already_executing",
                    "Дія вже виконується в Google — відмова зараз не скасує запит.",
                    op_id=op.op_id,
                    pending_state="executing",
                )
            return AgentResult("success", "Добре, скасовую цю дію.", {"op_id": op.op_id, "kind": op.kind})

        claimed = self._pending.begin_execute(sub, op.op_id)
        if claimed is None:
            again = self._pending.get(sub)
            if again and again.state == "completed" and again.execution_result:
                return AgentResult(
                    "success",
                    again.execution_result.get("message", "Вже виконано раніше."),
                    again.execution_result,
                )
            if again and again.state == "executing":
                return self._confirm_error(
                    "already_executing",
                    "Підтвердження вже виконується — зачекай результат.",
                    op_id=op.op_id,
                    pending_state="executing",
                )
            return self._confirm_error(
                "begin_execute_lost",
                "Не вдалося підтвердити. Нічого не повторюю, доки не буде нового запиту.",
                op_id=op.op_id,
            )
        if claimed.state == "completed" and claimed.execution_result:
            return AgentResult(
                "success",
                claimed.execution_result.get("message", "Вже виконано раніше."),
                claimed.execution_result,
            )
        if claimed.state == "ambiguous":
            return AgentResult(
                "ambiguous",
                claimed.ambiguous_reason or _AMBIGUOUS_MSG,
                {"op_id": claimed.op_id, "pending_state": "ambiguous"},
            )

        try:
            _sub, client = self._client_for_user()
            if _sub != sub:
                self._pending.clear(sub)
                return self._confirm_error(
                    "account_mismatch",
                    "Підтвердження не відповідає активному акаунту.",
                )
            if claimed.kind == "calendar_create":
                result = self._execute_create(client, claimed.payload)
            elif claimed.kind == "calendar_cancel":
                result = self._execute_cancel(sub, client, claimed.payload, session_id=claimed.session_id)
            else:
                result = self._execute_update(sub, client, claimed.payload, session_id=claimed.session_id)
        except GoogleApiError as exc:
            if _uncertain(exc) or (claimed.kind == "calendar_create" and exc.http_status == 409):
                reason = (
                    "Можливий конфлікт створення — перевір календар, не дублюй."
                    if exc.http_status == 409
                    else _AMBIGUOUS_MSG
                )
                self._pending.mark_ambiguous(sub, reason)
                return AgentResult(
                    "ambiguous",
                    reason,
                    {
                        "op_id": claimed.op_id,
                        "pending_state": "ambiguous",
                        "kind": claimed.kind,
                        "google_error": exc.code,
                    },
                )
            self._pending.clear(sub)
            mapped = result_from_google_error(exc)
            mapped.data = {**mapped.data, "op_id": claimed.op_id, "kind": claimed.kind}
            return mapped
        except OAuthError as exc:
            self._pending.clear(sub)
            return result_from_google_error(exc)

        result.data = {
            **result.data,
            "op_id": claimed.op_id,
            "kind": claimed.kind,
        }
        if result.status == "success":
            self._pending.mark_executed(sub, result.data | {"message": result.message})
        elif result.status != "ambiguous":
            current = self._pending.get(sub)
            if current and current.state == "executing" and current.op_id == claimed.op_id:
                self._pending.clear(sub)
        return result

    def _confirm_error(self, code: str, message: str, **data: object) -> AgentResult:
        logger.info(
            "calendar confirm reason=%s pending_state=%s op_match=%s session_match=%s",
            code,
            data.get("pending_state"),
            data.get("op_match"),
            data.get("session_match"),
        )
        return AgentResult("error", message, {"reason_code": code, **data})

    def _execute_create(self, client: CalendarClient, payload: dict) -> AgentResult:
        body = {
            "summary": payload["title"],
            "start": {"dateTime": payload["start"], "timeZone": payload.get("timezone") or self._timezone},
            "end": {"dateTime": payload["end"], "timeZone": payload.get("timezone") or self._timezone},
        }
        if payload.get("description"):
            body["description"] = payload["description"]
        idem = payload.get("idempotency_key")
        if idem:
            body["iCalUID"] = f"{idem}@voice-agent.local"
        created = client.create_event(body, conference=bool(payload.get("with_meet")))
        event_id = created.get("id")
        if not event_id:
            return AgentResult("error", "Google не підтвердив створення події.")
        fresh = client.get_event(event_id, calendar_id=payload.get("calendar_id") or _PRIMARY)
        if (fresh.get("summary") or "") != payload["title"]:
            return AgentResult("error", "Google не підтвердив створення події.")
        meet = created.get("hangoutLink") or fresh.get("hangoutLink")
        msg = (
            f"Готово. Подію «{payload['title']}» створено на "
            f"{speak_date(str(payload['date']), now=self._now())} о {speak_clock(str(payload['time']))}."
        )
        if meet:
            msg += " Додав посилання Google Meet."
        return AgentResult(
            "success",
            msg,
            {"event_id": event_id, "meet_url": meet, "idempotency_key": idem, "kind": "calendar_create"},
        )

    def _execute_cancel(
        self,
        sub: str,
        client: CalendarClient,
        payload: dict,
        *,
        session_id: str | None,
    ) -> AgentResult:
        event_id = payload["event_id"]
        calendar_id = payload.get("calendar_id") or _PRIMARY
        if calendar_id != _PRIMARY:
            return AgentResult("error", "Операція прив'язана до іншого календаря і не виконується.")
        try:
            live = client.get_event(event_id, calendar_id=calendar_id)
        except GoogleApiError as exc:
            if _uncertain(exc):
                return AgentResult(
                    "error",
                    "Не вдалося звірити подію перед видаленням. Нічого не видалено.",
                    {"google_error": exc.code, "event_id": event_id},
                )
            raise
        if _conflicts(payload.get("original") or {}, live):
            return AgentResult(
                "needs_more_info",
                "Подія змінилась у Google після перегляду. Скажи ще раз, що скасувати — старе підтвердження не застосовую.",
                {"event_id": event_id},
            )
        try:
            client.delete_event(event_id, calendar_id=calendar_id)
        except GoogleApiError as exc:
            if _uncertain(exc):
                logger.info(
                    "calendar cancel delete uncertain op event_id=%s google_error=%s — reconciling",
                    event_id,
                    exc.code,
                )
                return self._reconcile_cancel(
                    sub,
                    client,
                    payload,
                    op_id=None,
                    prior_google_error=exc.code,
                )
            raise
        try:
            leftover = client.get_event(event_id, calendar_id=calendar_id)
        except GoogleApiError as exc:
            if exc.http_status == 404 or exc.code == "not_found":
                return self._cancel_success(sub, payload, reconciled=False)
            if _uncertain(exc):
                return self._reconcile_cancel(
                    sub,
                    client,
                    payload,
                    op_id=None,
                    prior_google_error=exc.code,
                )
            raise
        if _google_event_gone(leftover):
            return self._cancel_success(sub, payload, reconciled=False, verify="cancelled")
        return self._reconcile_cancel(sub, client, payload, op_id=None)

    def _cancel_success(
        self,
        sub: str,
        payload: dict,
        *,
        reconciled: bool,
        verify: str | None = None,
        op_id: str | None = None,
        prior_google_error: str | None = None,
        mark_pending: bool = False,
    ) -> AgentResult:
        event_id = payload.get("event_id") or ""
        title = (payload.get("summary") or "").strip()
        self._forget_user(sub)
        message = f"Готово, «{title}» видалено!" if title else "Готово, подію видалено!"
        data: dict = {
            "event_id": event_id,
            "kind": "calendar_cancel",
            "reconciled": reconciled,
        }
        if verify:
            data["verify"] = verify
        if op_id:
            data["op_id"] = op_id
            data["pending_state"] = "completed"
        if prior_google_error:
            data["google_error"] = prior_google_error
        if mark_pending:
            self._pending.mark_executed(sub, data | {"message": message})
        return AgentResult("success", message, data)

    def _reconcile_cancel(
        self,
        sub: str,
        client: CalendarClient,
        payload: dict,
        *,
        op_id: str | None,
        prior_google_error: str | None = None,
    ) -> AgentResult:
        """After an uncertain delete, verify the exact event_id once — never delete again."""
        event_id = payload.get("event_id") or ""
        calendar_id = payload.get("calendar_id") or _PRIMARY
        title = (payload.get("summary") or "").strip() or "подію"
        data_base = {
            "event_id": event_id,
            "kind": "calendar_cancel",
            "reconciled": True,
        }
        if op_id:
            data_base["op_id"] = op_id
        if prior_google_error:
            data_base["google_error"] = prior_google_error
        try:
            leftover = client.get_event(event_id, calendar_id=calendar_id)
        except GoogleApiError as exc:
            if exc.http_status == 404 or exc.code == "not_found":
                result = self._cancel_success(
                    sub,
                    payload,
                    reconciled=True,
                    verify="absent",
                    op_id=op_id,
                    prior_google_error=prior_google_error,
                    mark_pending=True,
                )
                logger.info(
                    "calendar cancel reconciled deleted event_id=%s op_id=%s",
                    event_id,
                    op_id,
                )
                return result
            if _uncertain(exc):
                message = (
                    "Не можу точно підтвердити видалення. "
                    "Зараз не вдалося перевірити стан календаря."
                )
                self._pending.mark_ambiguous(sub, message)
                logger.info(
                    "calendar cancel reconcile unresolved event_id=%s google_error=%s",
                    event_id,
                    exc.code,
                )
                return AgentResult(
                    "ambiguous",
                    message,
                    {
                        **data_base,
                        "pending_state": "ambiguous",
                        "verify": "unresolved",
                        "verify_error": exc.code,
                    },
                )
            mapped = result_from_google_error(exc)
            mapped.data = {**mapped.data, **data_base, "verify": "error"}
            self._pending.clear(sub)
            return mapped

        if _google_event_gone(leftover):
            result = self._cancel_success(
                sub,
                payload,
                reconciled=True,
                verify="cancelled",
                op_id=op_id,
                prior_google_error=prior_google_error,
                mark_pending=True,
            )
            logger.info(
                "calendar cancel reconciled cancelled tombstone event_id=%s op_id=%s",
                event_id,
                op_id,
            )
            return result

        # Event still present — do not retry delete automatically.
        message = f"Не вдалося видалити «{title}». Подія все ще є в календарі."
        self._pending.clear(sub)
        logger.info("calendar cancel reconciled still_present event_id=%s", event_id)
        return AgentResult(
            "error",
            message,
            {**data_base, "pending_state": None, "verify": "still_present", "reason_code": "delete_not_applied"},
        )

    def _execute_update(
        self,
        sub: str,
        client: CalendarClient,
        payload: dict,
        *,
        session_id: str | None,
    ) -> AgentResult:
        event_id = payload["event_id"]
        calendar_id = payload.get("calendar_id") or _PRIMARY
        if calendar_id != _PRIMARY:
            return AgentResult("error", "Операція прив'язана до іншого календаря і не виконується.")
        try:
            live = client.get_event(event_id, calendar_id=calendar_id)
        except GoogleApiError as exc:
            if _uncertain(exc):
                return AgentResult(
                    "error",
                    "Не вдалося звірити подію перед зміною. Нічого не змінено.",
                    {"google_error": exc.code, "event_id": event_id},
                )
            raise
        if _conflicts(payload.get("original") or {}, live):
            return AgentResult(
                "needs_more_info",
                "Подія змінилась у Google після перегляду. Скажи ще раз, що змінити — старе підтвердження не застосовую.",
                {"event_id": event_id},
            )
        try:
            client.update_event(event_id, payload["patch"], calendar_id=calendar_id)
        except GoogleApiError as exc:
            if _uncertain(exc):
                self._pending.mark_ambiguous(sub, _AMBIGUOUS_MSG)
                return AgentResult(
                    "ambiguous",
                    _AMBIGUOUS_MSG,
                    {"pending_state": "ambiguous", "google_error": exc.code, "event_id": event_id},
                )
            raise
        try:
            fresh = client.get_event(event_id, calendar_id=calendar_id)
        except GoogleApiError as exc:
            if _uncertain(exc):
                self._pending.mark_ambiguous(sub, _AMBIGUOUS_MSG)
                return AgentResult(
                    "ambiguous",
                    _AMBIGUOUS_MSG,
                    {"pending_state": "ambiguous", "google_error": exc.code, "event_id": event_id},
                )
            raise
        if not _patch_applied(fresh, payload["patch"]):
            self._pending.mark_ambiguous(sub, _AMBIGUOUS_MSG)
            return AgentResult(
                "ambiguous",
                "Контрольне читання не підтвердило зміну. Перевір календар і не повторюй «так».",
                {"pending_state": "ambiguous", "event_id": event_id},
            )
        self._remember(sub, session_id, fresh)
        return AgentResult(
            "success",
            payload.get("success_message") or "Подію оновлено.",
            {"event_id": fresh.get("id") or event_id, "kind": "calendar_update"},
        )
