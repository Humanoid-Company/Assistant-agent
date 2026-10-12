"""Calendar agent — view/search/create/edit/cancel with human confirmation.

The flow is split across mixins: calendar_create (create), calendar_edit (edit/cancel),
calendar_execution (confirm + Google writes + reconcile). Pure helpers live in
calendar_speech / calendar_events / calendar_validation."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agents.calendar_create import CalendarCreateMixin
from agents.calendar_edit import CalendarEditMixin
from agents.calendar_events import (
    _is_past,
    _is_recurring,
    _local_parts,
    _timed_bounds,
)
from agents.calendar_execution import CalendarExecutionMixin
from agents.calendar_speech import (
    _mentioned_dates,
    _mentioned_times,  # noqa: F401  (re-export for tests)
    _strip_query_words,
    _week_window,
)
from agents.calendar_validation import (
    _NO,
    _PRIMARY,
    _YES,
    _blank,
    calendar_call_problem,
)
from agents.create_draft import (
    CreateDraftStore,
)
from agents.pending_store import PendingConflict, PendingStore
from agents.types import AgentResult, result_from_google_error
from auth.account_manager import AccountManager
from auth.google_oauth import OAuthError
from integrations.google_calendar import (
    CalendarClient,
    GoogleCalendarClient,
    parse_search_criteria,
    relative_or_iso_date,
    speak_date,
    speak_local_when,
)
from integrations.google_errors import GoogleApiError

logger = logging.getLogger(__name__)

# A listing names this many events and counts the rest; the backend model gets more of them as data
# (a university week is thirty classes).
_SPOKEN_EVENTS = 8
_PUBLIC_EVENTS = 20
# A choice of calendars («лише пари») holds this long; then a listing asks again.
_CHOICE_TTL_S = 30 * 60
_ALL_WORDS = ("all", "усі", "всі", "усіх", "всіх", "все", "усе", "обидва", "обидві", "будь")
_PRIMARY_STEMS = ("primary", "основн", "особист", "персонал", "мій", "мої", "мого", "моєму", "свій", "власн")


def _match_calendars(choice: str, calendars: list[dict]) -> frozenset[str] | None | bool:
    """«в усіх» → None (all), «особистий» → the primary one, «уні»/«університетський» → the calendar
    whose name has a word starting the same way. False — nothing matched."""
    words = [w for w in re.findall(r"[\w'’]+", choice.casefold()) if len(w) >= 3 or w in ("всі", "усі", "мій")]
    if any(w.startswith(_ALL_WORDS) for w in words):
        return None
    if any(w.startswith(_PRIMARY_STEMS) for w in words):
        return frozenset({"primary"})
    found = set()
    for calendar in calendars:
        if calendar["primary"]:
            continue
        name_words = [w for w in re.findall(r"[\w'’]+", calendar["name"].casefold()) if len(w) >= 3]
        if choice.casefold().strip() == calendar["name"].casefold().strip() or any(
            w.startswith(n) or n.startswith(w) for w in words for n in name_words
        ):
            found.add(calendar["id"])
    return frozenset(found) if found else False


class CalendarAgent(CalendarCreateMixin, CalendarEditMixin, CalendarExecutionMixin):
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
        # Which calendars a listing reads, as the user chose («в усіх», «лише пари»): sub → (ids or
        # None for all, when it stops counting). Live sessions end at every pause, so not per session.
        self._calendar_choice: dict[str, tuple[frozenset[str] | None, float]] = {}

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
        assistant_utterances: list[str] | None = None,
        reminder_minutes: int | None = None,
        calendar: str | None = None,
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
                "reminder_minutes": reminder_minutes,
                "calendar": calendar,
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
                ("reminder_minutes", reminder_minutes),
                ("calendar", calendar),
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
            assistant_utterances=assistant_utterances,
            reminder_minutes=reminder_minutes,
            calendar=calendar,
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
                return self.list_upcoming(
                    query=kwargs["query"], session_id=kwargs["session_id"], date=kwargs["date"],
                    calendar=kwargs["calendar"],
                )
            if action == "search":
                return self.search(
                    kwargs["query"] or kwargs["title"] or "",
                    date=kwargs["date"],
                    time=kwargs["time"],
                    session_id=kwargs["session_id"],
                    calendar=kwargs["calendar"],
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
                    assistant_utterances=kwargs.get("assistant_utterances"),
                )
                if isinstance(grounded, AgentResult):
                    return grounded
                return self.propose_create(
                    **grounded,
                    with_meet=kwargs["with_meet"],
                    session_id=kwargs["session_id"],
                    reminder_minutes=kwargs["reminder_minutes"],
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
                    reminder_minutes=kwargs["reminder_minutes"],
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

    def _guard_pending(self, sub: str, *, replaces: frozenset[str] = frozenset()) -> AgentResult | None:
        """replaces: kinds of a still unanswered preview that a new request of the same sort takes
        over — «тоді після пар» after a taken slot is a new plan, not a yes/no, and answering it
        with the old question again and again («система підставила тринадцяту») went nowhere."""
        op = self._pending.get(sub)
        if op is None:
            return None
        if op.state == "pending" and op.kind in replaces and self._pending.mark_cancelled(sub):
            logger.info("calendar pending superseded kind=%s op_id=%s", op.kind, op.op_id)
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
        if event.get("_calendar_id"):  # another calendar: read only, nothing to change later
            self._forget(sub, session_id)
            return
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

    def _read_query(
        self, query: str | None, now: datetime
    ) -> tuple[str, str | None, str | None, tuple[datetime, datetime] | None]:
        """«які в мене завтра плани» → no title filter + tomorrow. Question words, spoken dates
        («3 жовтня», «на суботу») and week ranges never end up as an event-name filter."""
        title, parsed_date, parsed_time = parse_search_criteria(query, now=now, timezone=self._timezone)
        window = None
        if parsed_date is None and query:
            window = _week_window(query, now)
            if window is None:
                spoken = _mentioned_dates(query, now)
                if len(spoken) == 1:
                    parsed_date = next(iter(spoken))
        return _strip_query_words(title), parsed_date, parsed_time, window

    def _day_of(self, value: str | None, now: datetime) -> str | None:
        """A date the model passed — ISO, «завтра» or «3 жовтня» → YYYY-MM-DD, else None."""
        text = _blank(value)
        if text is None:
            return None
        iso = relative_or_iso_date(text, now=now, timezone=self._timezone)
        if iso:
            return iso
        spoken = _mentioned_dates(text, now)
        return next(iter(spoken)) if len(spoken) == 1 else None

    def list_upcoming(
        self,
        query: str | None = None,
        days: int = 7,
        session_id: str | None = None,
        date: str | None = None,
        calendar: str | None = None,
    ) -> AgentResult:
        sub, client = self._client_for_user()
        scope = self._calendar_scope(sub, client, calendar, ask=True)
        if isinstance(scope, AgentResult):
            return scope
        now = self._now()
        title, parsed_date, parsed_time, window = self._read_query(query, now)
        day = self._day_of(date, now) or parsed_date
        span = dict(
            date=day,
            time=parsed_time,
            now=now,
            future_only=day is None,
            horizon_days=days,
            window=None if day else window,
            all_calendars=True,
            calendar_ids=scope,
        )
        events = self._collect(client, title=title, **span)
        if title and not events and (day or window):
            # «що в мене на цей тиждень по планах»: a leftover word is no event name — a question
            # about a period gets the period, not «нічого немає».
            everything = self._collect(client, title="", **span)
            if everything:
                result = self._events_result(sub, session_id, everything, search_mode=False)
                result.message = f"Подій із назвою «{title}» немає. " + result.message
                return result
        return self._events_result(sub, session_id, events, search_mode=bool(title))

    def search(
        self,
        query: str,
        *,
        date: str | None = None,
        time: str | None = None,
        session_id: str | None = None,
        calendar: str | None = None,
    ) -> AgentResult:
        if not (query or "").strip() and not date and not time:
            return AgentResult("needs_more_info", "Що саме шукати в календарі?")
        sub, client = self._client_for_user()
        scope = self._calendar_scope(sub, client, calendar, ask=False)  # a search looks everywhere
        if isinstance(scope, AgentResult):
            return scope
        now = self._now()
        title, parsed_date, parsed_time, window = self._read_query(query, now)
        day = self._day_of(date, now) or parsed_date
        events = self._collect(
            client,
            title=title,
            date=day,
            time=time or parsed_time,
            now=now,
            future_only=False,
            horizon_days=30,
            window=None if day else window,
            all_calendars=True,
            calendar_ids=scope,
        )
        return self._events_result(sub, session_id, events, search_mode=True)

    def _calendar_scope(
        self, sub: str, client: CalendarClient, calendar: str | None, *, ask: bool
    ) -> frozenset[str] | None | AgentResult:
        """Which calendars to read: None — all. With more than one and no choice yet, a listing asks
        («в усіх чи в якомусь одному?»); the answer counts for _CHOICE_TTL_S."""
        choice = _blank(calendar)
        if choice is None:
            remembered = self._calendar_choice.get(sub)
            if remembered and remembered[1] > time.monotonic():
                return remembered[0]
            if not ask:
                return None
        calendars = client.calendars()
        if len(calendars) <= 1:
            return None
        names = ", ".join(c["name"] if c["primary"] else f"«{c['name']}»" for c in calendars)
        if choice is None:
            return AgentResult(
                "needs_more_info",
                f"Подивитись в усіх календарях чи в якомусь одному? Є: {names}.",
                {"missing_fields": ["calendar"], "reason_code": "calendar_choice",
                 "calendars": [c["name"] for c in calendars]},
            )
        ids = _match_calendars(choice, calendars)
        if ids is False:
            return AgentResult(
                "needs_more_info",
                f"Не знайшла календар «{choice}». Є: {names}. Який із них?",
                {"missing_fields": ["calendar"], "reason_code": "calendar_not_found",
                 "calendars": [c["name"] for c in calendars]},
            )
        self._calendar_choice[sub] = (ids, time.monotonic() + _CHOICE_TTL_S)
        return ids

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
        public = [self._public_event(event) for event in events[:_PUBLIC_EVENTS]]
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
        lines = [self._spoken_line(event) for event in events[:_SPOKEN_EVENTS]]
        if len(events) > len(lines):
            lines.append(f"і ще {len(events) - len(lines)} (усього {len(events)})")
        if search_mode:
            message = (
                "Знайшов кілька подій: " + "; ".join(lines) + ". Уточни назву, дату й час. "
                "Нічого не видаляю і не змінюю."
            )
        else:
            message = "Ось найближчі події: " + "; ".join(lines)
        return AgentResult("success", message, {"events": public})

    def _overlapping(
        self, start: datetime, end: datetime, *, exclude_ids: tuple[str, ...] = ()
    ) -> list[dict]:
        """Timed events that overlap [start, end), in every calendar switched on (a class in the
        university timetable takes the slot too). All-day events don't block a time slot.
        A failed lookup never blocks the action — the check is a courtesy, not a gate."""
        try:
            _sub, client = self._client_for_user()
            # Look back a day so a long event that started earlier is caught too.
            events = client.list_all_events(start - timedelta(days=1), end)
        except (OAuthError, GoogleApiError):
            logger.warning("calendar overlap check failed", exc_info=True)
            return []
        found = []
        for event in events:
            if event.get("id") in exclude_ids or event.get("recurringEventId") in exclude_ids:
                continue
            bounds = _timed_bounds(event)
            if bounds and bounds[0] < end and bounds[1] > start:
                found.append(event)
        return found

    def _overlap_phrase(self, events: list[dict]) -> str:
        """«На цей час уже є «Бізнес» з 15:00 до 16:00.»"""
        zone = ZoneInfo(self._timezone)
        parts = []
        for event in events[:3]:
            begin, finish = _timed_bounds(event)  # type: ignore[misc]
            where = f" (календар «{event['_calendar_name']}», його не змінити)" if event.get("_calendar_name") else ""
            parts.append(
                f"«{event.get('summary') or '(без назви)'}» з {begin.astimezone(zone):%H:%M} "
                f"до {finish.astimezone(zone):%H:%M}{where}"
            )
        more = f" і ще {len(events) - 3}" if len(events) > 3 else ""
        return "На цей час уже є " + ", ".join(parts) + more + "."

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
            "calendar_id": event.get("_calendar_id") or _PRIMARY,
            **({"calendar": event["_calendar_name"], "read_only": True} if event.get("_calendar_id") else {}),
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
        bounds = _timed_bounds(event)
        if bounds:  # without the end she guessed it («до сімнадцятої п'ятдесят» for 19:50)
            when += f" до {bounds[1].astimezone(ZoneInfo(self._timezone)):%H:%M}"
        line = f"{when} — {event.get('summary') or '(без назви)'}"
        if event.get("_calendar_name"):
            line += f" (календар «{event['_calendar_name']}»)"
        return line

