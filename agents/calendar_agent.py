"""Calendar agent — view/search/create/edit/cancel with human confirmation.

The flow is split across mixins: calendar_create (create), calendar_edit (edit/cancel),
calendar_execution (confirm + Google writes + reconcile). Pure helpers live in
calendar_speech / calendar_events / calendar_validation."""
from __future__ import annotations

import logging
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
from agents.calendar_speech import _mentioned_times  # noqa: F401  (re-export for tests)
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

    def _overlapping(
        self, start: datetime, end: datetime, *, exclude_ids: tuple[str, ...] = ()
    ) -> list[dict]:
        """Timed events that overlap [start, end). All-day events don't block a time slot.
        A failed lookup never blocks the action — the check is a courtesy, not a gate."""
        try:
            _sub, client = self._client_for_user()
            # Look back a day so a long event that started earlier is caught too.
            events = client.list_events(start - timedelta(days=1), end)
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
            parts.append(
                f"«{event.get('summary') or '(без назви)'}» з {begin.astimezone(zone):%H:%M} "
                f"до {finish.astimezone(zone):%H:%M}"
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

