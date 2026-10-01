"""CalendarAgent: create-event flow (prepare, ground against what the user said, propose)."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agents.calendar_speech import (
    _CHOICE_TITLE_RE,
    _is_acknowledgement,
    _mentioned_times,
)
from agents.calendar_validation import (
    _PRIMARY,
    _blank,
    _clock_value,
    _missing_create,
)
from agents.create_draft import (
    best_title_span,
    is_draft_cancel,
    is_title_confirmation,
    title_said_by_user,
    title_similarity,
)
from agents.types import AgentResult, result_from_google_error
from auth.google_oauth import OAuthError
from integrations.google_calendar import (
    interpret_local_start,
    local_wall_time_problem,
    resolve_start_end,
    speak_clock,
    speak_date,
)


class CalendarCreateMixin:
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

