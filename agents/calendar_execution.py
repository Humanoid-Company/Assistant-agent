"""CalendarAgent: confirmation handling, Google writes and post-write reconciliation."""
from __future__ import annotations

import logging

from agents.calendar_events import (
    _conflicts,
    _google_event_gone,
    _patch_applied,
    _uncertain,
)
from agents.calendar_validation import (
    _AMBIGUOUS_MSG,
    _NO,
    _PRIMARY,
    _YES,
)
from agents.types import AgentResult, result_from_google_error
from auth.google_oauth import OAuthError
from integrations.google_calendar import (
    CalendarClient,
    speak_clock,
    speak_date,
)
from integrations.google_errors import GoogleApiError

logger = logging.getLogger(__name__)


class CalendarExecutionMixin:
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
