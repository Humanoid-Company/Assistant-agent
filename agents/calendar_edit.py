"""CalendarAgent: edit/cancel flow (resolve the target event, plan the change, propose)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agents.calendar_events import (
    _is_recurring,
    _local_parts,
    _mutation_id,
    _scope,
    _snapshot,
    _timed_bounds,
)
from agents.calendar_speech import (
    _DURATION_RE,
    _day_phrase,
    _duration_phrase,
    _is_deictic,
)
from agents.calendar_validation import (
    _PRIMARY,
)
from agents.types import AgentResult, result_from_google_error
from integrations.google_calendar import (
    CalendarClient,
    extract_move_phrases,
    local_wall_time_problem,
    parse_search_criteria,
    relative_or_iso_date,
    speak_clock,
    speak_date,
    speak_local_when,
    split_local_datetime,
    validate_date_time,
)
from integrations.google_errors import GoogleApiError


class CalendarEditMixin:
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

