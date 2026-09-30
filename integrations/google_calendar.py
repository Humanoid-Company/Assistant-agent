"""Thin Google Calendar API wrapper (mockable)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from integrations.google_errors import GoogleApiError, map_google_error

logger = logging.getLogger(__name__)


class CalendarClient(Protocol):
    def list_events(self, time_min: datetime, time_max: datetime, query: str | None = None) -> list[dict]: ...
    def create_event(self, body: dict, *, conference: bool = False) -> dict: ...
    def update_event(self, event_id: str, body: dict, calendar_id: str | None = None) -> dict: ...
    def delete_event(self, event_id: str, calendar_id: str | None = None) -> None: ...
    def get_event(self, event_id: str, calendar_id: str | None = None) -> dict: ...


class GoogleCalendarClient:
    def __init__(self, credentials, calendar_id: str = "primary") -> None:
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self._calendar_id = calendar_id

    def list_events(self, time_min: datetime, time_max: datetime, query: str | None = None) -> list[dict]:
        try:
            kwargs: dict[str, Any] = {
                "calendarId": self._calendar_id,
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 50,
            }
            if query:
                kwargs["q"] = query
            response = self._service.events().list(**kwargs).execute()
            return response.get("items", [])
        except Exception as exc:
            raise map_google_error(exc) from exc

    def create_event(self, body: dict, *, conference: bool = False) -> dict:
        try:
            if conference:
                body = {
                    **body,
                    "conferenceData": {
                        "createRequest": {
                            "requestId": str(uuid4()),
                            "conferenceSolutionKey": {"type": "hangoutsMeet"},
                        }
                    },
                }
            return (
                self._service.events()
                .insert(
                    calendarId=self._calendar_id,
                    body=body,
                    conferenceDataVersion=1 if conference else 0,
                    sendUpdates="all",
                )
                .execute()
            )
        except Exception as exc:
            raise map_google_error(exc) from exc

    def update_event(self, event_id: str, body: dict, calendar_id: str | None = None) -> dict:
        try:
            return (
                self._service.events()
                .patch(
                    calendarId=calendar_id or self._calendar_id,
                    eventId=event_id,
                    body=body,
                    sendUpdates="all",
                )
                .execute()
            )
        except Exception as exc:
            raise map_google_error(exc) from exc

    def delete_event(self, event_id: str, calendar_id: str | None = None) -> None:
        try:
            self._service.events().delete(
                calendarId=calendar_id or self._calendar_id,
                eventId=event_id,
                sendUpdates="all",
            ).execute()
        except Exception as exc:
            raise map_google_error(exc) from exc

    def get_event(self, event_id: str, calendar_id: str | None = None) -> dict:
        try:
            return (
                self._service.events()
                .get(calendarId=calendar_id or self._calendar_id, eventId=event_id)
                .execute()
            )
        except Exception as exc:
            raise map_google_error(exc) from exc


class FakeCalendarClient:
    """In-memory calendar for tests. Records the exact Google calls the agent makes."""

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.fail_with: Exception | None = None
        self.fail_on: dict[str, Exception] = {}
        self.raise_after_create: Exception | None = None
        self.raise_after_update: Exception | None = None
        self.raise_after_delete: Exception | None = None
        self.fail_get_on_call: int | None = None
        self.create_calls = 0
        self.list_queries: list[str | None] = []
        self.update_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.get_calls: list[str] = []

    def _raise_if(self, method: str) -> None:
        if method in self.fail_on:
            raise self.fail_on[method]
        if self.fail_with:
            raise self.fail_with

    def list_events(self, time_min: datetime, time_max: datetime, query: str | None = None) -> list[dict]:
        self.list_queries.append(query)
        self._raise_if("list")
        items = []
        for event in self.events.values():
            if str(event.get("status") or "").strip().lower() == "cancelled":
                continue
            start_dt = _event_instant(event)
            if start_dt is None:
                continue
            if start_dt < _as_utc(time_min) or start_dt >= _as_utc(time_max):
                continue
            if query and not _query_matches(query, event):
                continue
            items.append(event)
        items.sort(key=lambda event: _event_instant(event) or datetime.min.replace(tzinfo=ZoneInfo("UTC")))
        return items

    def create_event(self, body: dict, *, conference: bool = False) -> dict:
        self._raise_if("create")
        # Idempotent re-insert with same iCalUID returns the existing event.
        ical = body.get("iCalUID")
        if ical:
            for existing in self.events.values():
                if existing.get("iCalUID") == ical:
                    return existing
        self.create_calls += 1
        event_id = body.get("id") or f"evt-{self.create_calls}"
        event = {
            **body,
            "id": event_id,
            "etag": body.get("etag") or "etag-1",
            "htmlLink": f"https://calendar.example/{event_id}",
        }
        if conference:
            event["hangoutLink"] = f"https://meet.google.com/{event_id}"
        self.events[event_id] = event
        if self.raise_after_create:
            exc = self.raise_after_create
            self.raise_after_create = None
            raise exc
        return event

    def update_event(self, event_id: str, body: dict, calendar_id: str | None = None) -> dict:
        del calendar_id
        self.update_calls.append((event_id, dict(body)))
        self._raise_if("update")
        if event_id not in self.events:
            raise GoogleApiError("not_found", 404, "Подію не знайдено.")
        merged = {**self.events[event_id], **body, "id": event_id}
        merged["etag"] = _next_etag(self.events[event_id].get("etag"))
        self.events[event_id] = merged
        if self.raise_after_update:
            exc = self.raise_after_update
            self.raise_after_update = None
            raise exc
        return merged

    def delete_event(self, event_id: str, calendar_id: str | None = None) -> None:
        del calendar_id
        self.delete_calls.append(event_id)
        self._raise_if("delete")
        doomed = [
            key
            for key, event in self.events.items()
            if key == event_id or event.get("recurringEventId") == event_id
        ]
        if not doomed:
            raise GoogleApiError("not_found", 404, "Подію не знайдено.")
        # Mirror Google soft-delete: get() still returns the row with status=cancelled.
        for key in doomed:
            self.events[key] = {**self.events[key], "status": "cancelled"}
        if self.raise_after_delete:
            exc = self.raise_after_delete
            self.raise_after_delete = None
            raise exc

    def get_event(self, event_id: str, calendar_id: str | None = None) -> dict:
        del calendar_id
        self.get_calls.append(event_id)
        if self.fail_get_on_call is not None and len(self.get_calls) == self.fail_get_on_call:
            raise GoogleApiError("timeout", None, "timeout")
        self._raise_if("get")
        if event_id not in self.events:
            raise GoogleApiError("not_found", 404, "Подію не знайдено.")
        return self.events[event_id]

    def is_active(self, event_id: str) -> bool:
        event = self.events.get(event_id)
        if not event:
            return False
        return str(event.get("status") or "").strip().lower() != "cancelled"


def resolve_start_end(date: str, time: str, duration_minutes: int, timezone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone)
    start = datetime.fromisoformat(f"{date}T{time}:00").replace(tzinfo=tz)
    return start, start + timedelta(minutes=duration_minutes)


def validate_date_time(date: str | None, time: str | None) -> str | None:
    """Return an error message if date/time are missing or invalid; else None.

    Rejects regex-looking but calendar-invalid values like 2099-02-30 or 25:90.
    """
    if not date or not time:
        return "Потрібні дата (РРРР-ММ-ДД) і час (ГГ:ХХ)."
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return "Дата має бути у форматі РРРР-ММ-ДД."
    if not re.fullmatch(r"\d{2}:\d{2}", time):
        return "Час має бути у форматі ГГ:ХХ."
    try:
        hour, minute = int(time[:2]), int(time[3:])
    except ValueError:
        return "Некоректний час."
    if hour > 23 or minute > 59:
        return f"Некоректний час «{time}». Година 00–23, хвилини 00–59."
    try:
        datetime.fromisoformat(f"{date}T{time}:00")
    except ValueError:
        return f"Некоректна дата «{date}». Перевір день і місяць."
    return None


def format_local(iso_value: str, timezone: str) -> str:
    dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone))
    return dt.strftime("%Y-%m-%d %H:%M")


_MONTHS_GENITIVE = (
    "",
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
)


def speak_date(date: str, *, now: datetime | None = None) -> str:
    """Voice-friendly date: '30 вересня', or 'сьогодні'/'завтра' when `now` is set. No year."""
    text = (date or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    year, month, day = (int(part) for part in text.split("-"))
    if now is not None:
        local = now if now.tzinfo is None else now
        today = local.strftime("%Y-%m-%d")
        tomorrow = (local + timedelta(days=1)).strftime("%Y-%m-%d")
        if text == today:
            return "сьогодні"
        if text == tomorrow:
            return "завтра"
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return text
    return f"{day} {_MONTHS_GENITIVE[month]}"


def speak_clock(time: str) -> str:
    """'19:00' → '19:00'; strip a leading zero on the hour for clearer TTS."""
    text = (time or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return text
    return f"{int(match.group(1))}:{match.group(2)}"


def speak_local_when(iso_value: str, timezone: str, *, now: datetime | None = None) -> str:
    """'2026-10-05T16:00:00+03:00' → '5 жовтня о 16:00'."""
    raw = (iso_value or "").strip()
    if "T" not in raw and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return speak_date(raw, now=now)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone))
    return f"{speak_date(dt.strftime('%Y-%m-%d'), now=now)} о {speak_clock(dt.strftime('%H:%M'))}"


def local_wall_time_problem(date: str | None, time: str | None, timezone: str) -> str | None:
    """Reject missing, impossible, nonexistent (DST gap) and ambiguous local times."""
    basic = validate_date_time(date, time)
    if basic:
        return basic
    assert date is not None and time is not None
    tz = ZoneInfo(timezone)
    naive = datetime.fromisoformat(f"{date}T{time}:00")
    earlier = naive.replace(tzinfo=tz, fold=0)
    roundtrip = earlier.astimezone(ZoneInfo("UTC")).astimezone(tz)
    if roundtrip.replace(tzinfo=None) != naive:
        return (
            f"Час {time} {date} не існує в поясі {timezone} через переведення годинника."
        )
    later = naive.replace(tzinfo=tz, fold=1)
    if earlier.utcoffset() != later.utcoffset():
        return (
            f"Час {time} {date} неоднозначний у поясі {timezone} через переведення годинника. Уточни час."
        )
    return None


_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_RELATIVE_DAYS = (("післязавтра", 2), ("сьогодні", 0), ("завтра", 1))
_COMMAND_RE = re.compile(
    r"\b(перенеси|перенести|пересунь|пересунути|зміни|змінити|скасуй|скасувати|"
    r"видали|видалити|відміни|постав|зроби|додай|додати|назву|опис|тривалість|"
    r"подію|подія|події|зустріч|зустрічі|календар|календаря|"
    r"сьогодні|завтра|післязавтра|підтвердити|підтверджуєш|будь|ласка)\b",
    re.I,
)
_PREPOSITION_RE = re.compile(r"\b(о|в|у|на|з|із|зі|та|і|й)\b", re.I)
_MOVE_SPAN_RE = re.compile(r"з\s+(\d{1,2}:\d{2})\s+на\s+(\d{1,2}:\d{2})", re.I)
_DEST_SPAN_RE = re.compile(
    r"на\s+(післязавтра|завтра|сьогодні|\d{4}-\d{2}-\d{2})\s+(?:о|в|у)\s+(\d{1,2}:\d{2})",
    re.I,
)


def normalize_hhmm(value: str) -> str:
    hour, minute = value.split(":", 1)
    return f"{int(hour):02d}:{minute}"


_OFFSET_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$", re.IGNORECASE)


def interpret_local_start(value: str | None, timezone: str) -> tuple[str | None, str | None, str | None]:
    """Split a start into a wall-clock date and HH:MM in ``timezone``.

    ``2026-09-29T16:00`` has no offset and is the user's local time, not UTC.
    A value that already has ``Z`` or a numeric offset is converted into that timezone.
    """
    if value is None or not str(value).strip():
        return None, None, None
    text = str(value).strip().replace(" ", "T")
    if _OFFSET_RE.search(text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None, None, "Некоректний час початку. Потрібен формат РРРР-ММ-ДДTГГ:ХХ."
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        local = parsed.astimezone(ZoneInfo(timezone))
        return local.strftime("%Y-%m-%d"), local.strftime("%H:%M"), None
    date, clock = split_local_datetime(text)
    if not date or not clock:
        return None, None, "Час початку має бути у форматі РРРР-ММ-ДДTГГ:ХХ за локальним часом."
    return date, clock, None


def split_local_datetime(value: str | None) -> tuple[str | None, str | None]:
    """Parse YYYY-MM-DD, YYYY-MM-DDTHH:MM or YYYY-MM-DD HH:MM."""
    if not value or not str(value).strip():
        return None, None
    text = str(value).strip().replace(" ", "T")
    if "T" not in text:
        if _ISO_DATE_RE.fullmatch(text):
            return text, None
        return None, None
    date_part, time_part = text.split("T", 1)
    if not _ISO_DATE_RE.fullmatch(date_part):
        return None, None
    match = _TIME_RE.match(time_part)
    if not match:
        return date_part, None
    return date_part, f"{int(match.group(1)):02d}:{match.group(2)}"


def extract_move_phrases(text: str | None) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Pull «з 15:00 на 16:00» and «на завтра о 14:30» out of a command.

    Returns (remaining_text, old_time, new_time, new_date_token, new_time_from_dest).
    new_date_token is YYYY-MM-DD or a relative word.
    """
    raw = text or ""
    old_time = new_time = None
    match = _MOVE_SPAN_RE.search(raw)
    if match:
        old_time = normalize_hhmm(match.group(1))
        new_time = normalize_hhmm(match.group(2))
        raw = raw[: match.start()] + " " + raw[match.end() :]
    dest_token = dest_time = None
    dest = _DEST_SPAN_RE.search(raw)
    if dest:
        dest_token = dest.group(1)
        dest_time = normalize_hhmm(dest.group(2))
        raw = raw[: dest.start()] + " " + raw[dest.end() :]
    return raw, old_time, new_time, dest_token, dest_time


def parse_search_criteria(
    text: str | None,
    *,
    now: datetime,
    timezone: str,
    ignore_dates: tuple[str, ...] = (),
    ignore_times: tuple[str, ...] = (),
) -> tuple[str, str | None, str | None]:
    """Split a command into title words, a calendar date and a clock time.

    The returned title is what may be sent as Google ``q``. Dates and times are not part of it.
    """
    raw = text or ""
    for date in ignore_dates:
        if date:
            raw = raw.replace(date, " ")
    for clock in ignore_times:
        if clock and ":" in clock:
            hour, minute = clock.split(":", 1)
            raw = re.sub(rf"(?<!\d)0?{int(hour)}:{minute}(?!\d)", " ", raw)
    found_date = None
    date_match = _ISO_DATE_RE.search(raw)
    if date_match:
        found_date = date_match.group(1)
        raw = raw[: date_match.start()] + " " + raw[date_match.end() :]
    else:
        lowered = raw.lower()
        zone_now = now.astimezone(ZoneInfo(timezone))
        for word, delta in _RELATIVE_DAYS:
            if word in lowered:
                found_date = (zone_now + timedelta(days=delta)).strftime("%Y-%m-%d")
                raw = re.sub(word, " ", raw, flags=re.I)
                break
    found_time = None
    time_match = _TIME_RE.search(raw)
    if time_match:
        found_time = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
        raw = raw[: time_match.start()] + " " + raw[time_match.end() :]
    title = _COMMAND_RE.sub(" ", raw)
    title = _PREPOSITION_RE.sub(" ", title)
    title = re.sub(r"[^\w\s'’ʼ-]+", " ", title, flags=re.UNICODE)
    title = re.sub(r"\s+", " ", title).strip()
    return title, found_date, found_time


def relative_or_iso_date(token: str | None, *, now: datetime, timezone: str) -> str | None:
    if not token:
        return None
    if _ISO_DATE_RE.fullmatch(token):
        return token
    zone_now = now.astimezone(ZoneInfo(timezone))
    for word, delta in _RELATIVE_DAYS:
        if token.lower() == word:
            return (zone_now + timedelta(days=delta)).strftime("%Y-%m-%d")
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Europe/Kyiv"))
    return value.astimezone(ZoneInfo("UTC"))


def _event_instant(event: dict) -> datetime | None:
    raw = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date")
    if not raw:
        return None
    if "T" not in str(raw):
        return datetime.fromisoformat(str(raw)).replace(tzinfo=ZoneInfo("Europe/Kyiv")).astimezone(ZoneInfo("UTC"))
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Kyiv"))
    return parsed.astimezone(ZoneInfo("UTC"))


def _query_matches(query: str, event: dict) -> bool:
    haystack = f"{event.get('summary') or ''} {event.get('description') or ''}".casefold()
    tokens = [part.casefold() for part in query.split() if part.strip()]
    if not tokens:
        return True
    return all(token in haystack for token in tokens)


def _next_etag(current: str | None) -> str:
    text = str(current or "etag-0")
    try:
        number = int(text.rsplit("-", 1)[-1]) + 1
    except ValueError:
        number = 1
    return f"etag-{number}"
