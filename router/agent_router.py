"""In-process Agent Router — replaces n8n webhook + external agent-ecosystem hop."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from agents.calendar_agent import CalendarAgent, calendar_call_problem
from agents.gmail_agent import GmailAgent
from agents.notes_agent import NotesAgent
from agents.pending_store import PendingStore
from agents.types import AgentResult
from auth.account_manager import AccountManager
from auth.google_oauth import OAuthError
from integrations.google_http import warm_up as warm_up_connection

logger = logging.getLogger(__name__)

_YES = re.compile(r"^\s*(так|да|yes|підтверджую|confirm|згоден|згодна)\s*[.!?]?\s*$", re.I)
_NO = re.compile(r"^\s*(ні|нет|no|скасуй|не треба|cancel|reject)\s*[.!?]?\s*$", re.I)

IntentRoute = Callable[[str, str, str | None], AgentResult]

# Free-text (dispatch_task) keyword tables, lowercase substrings.
_CONNECT_WORDS = ("підключи google", "підключити google", "увійди в google", "connect google", "авторизуй google")
_GRANT_GMAIL_WORDS = ("дай доступ до gmail", "дозвіл gmail", "підключи gmail", "доступ до пошти", "grant gmail")
_GRANT_NOTES_WORDS = (
    "дай доступ до нотаток",
    "доступ до нотаток",
    "дозвіл нотатки",
    "підключи нотатки",
    "grant notes",
    "доступ до google drive",
)
_DISCONNECT_WORDS = ("відключи google", "вийди з google", "disconnect google")
_SWITCH_WORDS = ("зміни google акаунт", "перемкни google", "інший google акаунт", "switch google account")
_LOCK_WORDS = ("заблокуй сесію", "заблокуй google", "lock session")
_STATUS_WORDS = ("статус google", "хто підключений", "google статус")
_NOTES_WORDS = (
    "запиши нотат",
    "занотуй",
    "запам'ятай",
    "запам’ятай",
    "додай до нотат",
    "мої нотат",
    "знайди нотат",
    "що я записував",
    "прочитай нотат",
    "останні нотат",
    "скільки нотат",
    "видали нотат",
    "зміни нотат",
    "допиши",
    "перейменуй нотат",
)
_GMAIL_WORDS = ("лист", "пошт", "gmail", "email", "чернетк", "надішли", "напиши на")
_CALENDAR_WORDS = ("календар", "зустріч", "нагадування", "подія", "розклад", "скасуй зустріч", "перенеси")

# Cheap authorized GETs, one per API host the agents talk to (Drive shares www.googleapis.com).
_WARM_UP_PROBES: tuple[tuple[dict[str, bool], str], ...] = (
    ({"calendar": True}, "https://www.googleapis.com/calendar/v3/users/me/calendarList/primary?fields=id"),
    ({"gmail_readonly": True}, "https://gmail.googleapis.com/gmail/v1/users/me/profile"),
)


class AgentRouter:
    def __init__(
        self,
        accounts: AccountManager,
        calendar: CalendarAgent,
        gmail: GmailAgent,
        pending: PendingStore,
        notes: NotesAgent | None = None,
    ) -> None:
        self.accounts = accounts
        self.calendar = calendar
        self.gmail = gmail
        self.pending = pending
        self.notes = notes or NotesAgent(accounts)
        self._intents: tuple[tuple[tuple[str, ...], IntentRoute], ...] = (
            (_CONNECT_WORDS, self._route_connect),
            (_GRANT_GMAIL_WORDS, lambda raw, lower, sid: self.grant_gmail()),
            (_GRANT_NOTES_WORDS, lambda raw, lower, sid: self.grant_notes()),
            (_DISCONNECT_WORDS, lambda raw, lower, sid: self.disconnect_google()),
            (_SWITCH_WORDS, lambda raw, lower, sid: self.reauth_switch()),
            (_LOCK_WORDS, lambda raw, lower, sid: self.lock_session()),
            (_STATUS_WORDS, lambda raw, lower, sid: self.google_status()),
            (_NOTES_WORDS, self._route_notes),
            (_GMAIL_WORDS, self._route_gmail),
            (_CALENDAR_WORDS, self._route_calendar),
        )

    def handle_text(self, text: str, *, session_id: str | None = None) -> AgentResult:
        """Free-text entry (dispatch_task compatibility). Prefer typed tools when possible."""
        raw = (text or "").strip()
        if not raw:
            return AgentResult("needs_more_info", "Не почув завдання — повтори, будь ласка.")

        try:
            sub = self.accounts.require_active_sub()
            pending = self.pending.get(sub)
            # Only treat true awaiting-confirmation ops; completed/ambiguous still block silent cancel.
            if pending is not None and pending.state not in ("pending", "executing", "completed", "ambiguous"):
                pending = None
        except OAuthError:
            pending = None
            sub = None

        if pending is not None and pending.state == "pending":
            if session_id and pending.session_id and pending.session_id != session_id:
                return AgentResult(
                    "error",
                    "Підтвердження належить іншій сесії. Скасуйте дію або завершіть у тій самій розмові.",
                )
            if _YES.match(raw):
                return self._confirm_pending(
                    pending.kind, "yes", session_id=session_id, op_id=pending.op_id
                )
            if _NO.match(raw):
                return self._confirm_pending(
                    pending.kind, "no", session_id=session_id, op_id=pending.op_id
                )
            # New command while confirmation is open — never silently cancel / rewrite the op.
            return AgentResult(
                "needs_more_info",
                f"Зараз очікую підтвердження: {pending.summary_uk} "
                "Спочатку скажіть «так» або «ні» (або «скасуй»), щоб завершити цю дію. "
                "Нову команду (наприклад доступ до Gmail) виконаю після цього.",
                {"pending_op_id": pending.op_id, "pending_kind": pending.kind},
            )

        if pending is not None and pending.state in ("completed", "ambiguous", "executing"):
            if _YES.match(raw) or _NO.match(raw):
                return self._confirm_pending(
                    pending.kind,
                    "yes" if _YES.match(raw) else "no",
                    session_id=session_id,
                    op_id=pending.op_id,
                )

        lower = raw.lower()
        for keywords, route in self._intents:
            if any(k in lower for k in keywords):
                return route(raw, lower, session_id)
        return AgentResult(
            "needs_more_info",
            "Можу допомогти з Google Календарем, Gmail і нотатками. Уточни завдання або підключи Google-акаунт.",
        )

    # ── Free-text intents (dispatch_task) — checked in table order, first match wins ──

    def _route_connect(self, raw: str, lower: str, session_id: str | None) -> AgentResult:
        return self.connect_google(with_gmail="gmail" in lower or "пошт" in lower)

    def _route_notes(self, raw: str, lower: str, session_id: str | None) -> AgentResult:
        if any(k in lower for k in ("скільки нотат", "скільки запис")):
            return self.notes.handle("count")
        if any(k in lower for k in ("видали", "прибери")):
            q = re.sub(
                r".*?(видали|прибери)\s+(нотатку|запис)?\s*(про)?\s*",
                "",
                raw,
                count=1,
                flags=re.I,
            ).strip(" ?.!")
            return self.notes.handle("delete", target=q or "остання", query=q or None)
        if any(k in lower for k in ("допиши", "доповни")):
            return self.notes.handle(
                "append",
                target="остання" if "останн" in lower else None,
                query=raw,
                append_text=raw,
            )
        if any(k in lower for k in ("зміни", "відредагуй", "заміни", "перейменуй")):
            return self.notes.handle("update", query=raw, target=raw, content=raw)
        if any(k in lower for k in ("знайди", "що я записував", "чи я щось записував", "про ")):
            q = re.sub(
                r".*?(знайди|записував про|про)\s+",
                "",
                raw,
                count=1,
                flags=re.I,
            ).strip(" ?.!")
            return self.notes.handle("search", query=q or raw)
        if any(k in lower for k in ("прочитай", "останн", "які в мене", "список")):
            return self.notes.handle("read", limit=5)
        content = re.sub(
            r"^(запиши нотатку|запиши ідею|занотуй|запам'ятай|запам’ятай|"
            r"додай до моїх нотаток|збережи ідею)\s*:?\s*",
            "",
            raw,
            flags=re.I,
        ).strip()
        return self.notes.handle("add", content=content or raw)

    def _route_gmail(self, raw: str, lower: str, session_id: str | None) -> AgentResult:
        if "знайди" in lower or "пошук" in lower or "шукай" in lower:
            q = re.sub(r".*?(знайди|пошук|шукай)\s+", "", raw, flags=re.I).strip() or raw
            return self.gmail.handle("search", query=q)
        return AgentResult(
            "needs_more_info",
            "Для пошти потрібні чіткі поля: одержувач, тема і текст — або скажи «знайди листи …».",
        )

    def _route_calendar(self, raw: str, lower: str, session_id: str | None) -> AgentResult:
        if any(k in lower for k in ("скасуй", "відміни", "видали", "видалі")):
            return self.calendar.handle("cancel", query=raw, session_id=session_id)
        if any(k in lower for k in ("перенес", "пересунь", "зміни", "переймен", "тривалість", "опис")):
            return self.calendar.handle("edit", query=raw, session_id=session_id)
        if any(k in lower for k in ("що у мене", "розклад", "які зустрічі", "покажи календар")):
            return self.calendar.handle("list", session_id=session_id)
        return AgentResult(
            "needs_more_info",
            "Щоб створити подію, назви тему, дату (РРРР-ММ-ДД) і час (ГГ:ХХ).",
        )

    def _confirm_pending(
        self,
        kind: str,
        confirmation: str,
        *,
        session_id: str | None = None,
        op_id: str | None = None,
    ) -> AgentResult:
        if kind.startswith("calendar"):
            return self.calendar.handle(
                "confirm",
                confirmation=confirmation,
                session_id=session_id,
                op_id=op_id,
            )
        if kind.startswith("gmail"):
            return self.gmail.handle(
                "confirm",
                confirmation=confirmation,
                session_id=session_id,
                op_id=op_id,
            )
        return AgentResult("error", "Невідомий тип очікуваної дії.")

    def connect_google(self, *, with_gmail: bool = False) -> AgentResult:
        attempt = self.accounts.connect(with_calendar=True, with_gmail=with_gmail)
        status = attempt.status
        if not attempt.ok:
            code = (
                "permission_denied"
                if ("відхил" in attempt.message.lower() or "скасован" in attempt.message.lower())
                else "auth_required"
            )
            return AgentResult(
                code,  # type: ignore[arg-type]
                attempt.message,
                {
                    "auth_ok": False,
                    "still_connected": status.connected,
                    "email": status.email,
                    "sub": status.active_sub,
                },
            )
        return AgentResult(
            "success",
            attempt.message,
            {
                "auth_ok": True,
                "email": status.email,
                "sub": status.active_sub,
                "calendar_ready": status.calendar_ready,
                "gmail_ready": status.gmail_ready,
                "notes_ready": status.notes_ready,
                "granted_scopes": status.granted_scopes,
            },
        )

    def grant_gmail(self) -> AgentResult:
        try:
            attempt = self.accounts.request_gmail_permission()
        except OAuthError as exc:
            from agents.types import result_from_google_error

            return result_from_google_error(exc)
        if not attempt.ok:
            return AgentResult(
                "permission_denied",
                attempt.message,
                {"auth_ok": False, "gmail_ready": attempt.status.gmail_ready},
            )
        return AgentResult(
            "success",
            attempt.message,
            {
                "auth_ok": True,
                "permission_granted": True,
                "gmail_ready": attempt.status.gmail_ready,
                "granted_scopes": attempt.status.granted_scopes,
            },
        )

    def grant_notes(self) -> AgentResult:
        try:
            attempt = self.accounts.request_notes_permission()
        except OAuthError as exc:
            from agents.types import result_from_google_error

            return result_from_google_error(exc)
        if not attempt.ok:
            return AgentResult(
                "permission_denied",
                attempt.message,
                {"auth_ok": False, "notes_ready": attempt.status.notes_ready},
            )
        return AgentResult(
            "success",
            attempt.message,
            {
                "auth_ok": True,
                "permission_granted": True,
                "notes_ready": attempt.status.notes_ready,
                "granted_scopes": attempt.status.granted_scopes,
            },
        )

    def reauth_switch(self) -> AgentResult:
        """Change account only via fresh browser OAuth — never by spoken email."""
        previous = None
        try:
            previous = self.accounts.require_active_sub()
        except OAuthError:
            previous = None
        attempt = self.accounts.switch_via_reauth()
        if previous:
            self.calendar.clear_conversation_state(previous)
            self.pending.clear(previous)
            self.notes.clear_cache(previous)
        if not attempt.ok:
            return AgentResult(
                "permission_denied"
                if ("відхил" in attempt.message.lower() or "скасован" in attempt.message.lower())
                else "auth_required",
                attempt.message,
                {
                    "auth_ok": False,
                    "still_connected": attempt.status.connected,
                    "email": attempt.status.email,
                },
            )
        if attempt.status.active_sub and attempt.status.active_sub != previous:
            self.calendar.clear_conversation_state(attempt.status.active_sub)
            self.notes.clear_cache(attempt.status.active_sub)
        return AgentResult(
            "success",
            "Активний акаунт оновлено через браузер. " + attempt.message,
            {"auth_ok": True, "email": attempt.status.email, "sub": attempt.status.active_sub},
        )

    def lock_session(self) -> AgentResult:
        previous = None
        try:
            previous = self.accounts.require_active_sub()
        except OAuthError:
            previous = None
        status = self.accounts.lock_session()
        if previous:
            self.calendar.clear_conversation_state(previous)
            self.pending.clear(previous)
            self.notes.clear_cache(previous)
        return AgentResult("success", status.message, {"session_locked": True, "connected": False})

    def disconnect_google(self) -> AgentResult:
        previous = None
        try:
            previous = self.accounts.require_active_sub()
        except OAuthError:
            previous = None
        status = self.accounts.disconnect()
        if previous:
            self.calendar.clear_conversation_state(previous)
            self.pending.clear(previous)
            self.notes.clear_cache(previous)
        return AgentResult("success", status.message)

    def google_status(self) -> AgentResult:
        status = self.accounts.status()
        data = {
            "connected": status.connected,
            "email": status.email,
            "calendar_ready": status.calendar_ready,
            "gmail_readonly_ready": status.gmail_readonly_ready,
            "gmail_compose_ready": status.gmail_compose_ready,
            "gmail_send_ready": status.gmail_send_ready,
            "gmail_ready": status.gmail_ready,
            "notes_ready": status.notes_ready,
            "granted_scopes": status.granted_scopes,
            "accounts": status.accounts,
            "session_locked": status.session_locked,
            "last_auth_ok": status.last_auth_ok,
        }
        if not status.connected:
            return AgentResult("auth_required", status.message, data)
        return AgentResult("success", status.message, data)

    def check_connection(self) -> AgentResult:
        status = self.accounts.status()
        if not status.connected:
            return AgentResult(
                "auth_required",
                "Google-акаунт ще не підключено — це не аварія сервера. Скажіть «підключи Google».",
                {"connected": False},
            )
        if "відкликан" in status.message.lower() or "прострочен" in status.message.lower():
            return AgentResult("auth_required", status.message, {"connected": True, "revoked": True})
        if status.calendar_ready:
            try:
                probe = self.calendar.list_upcoming(days=1)
                if probe.status == "error":
                    return AgentResult("error", probe.message, {"google_api": False})
            except Exception as exc:
                logger.warning("Connection probe failed: %s", type(exc).__name__)
                return AgentResult("error", "Немає мережі або Google API недоступний.", {"network": False})
        return AgentResult("success", status.message, _asdict_safe(status))

    def warm_up(self) -> None:
        """Best-effort, off the audio thread, right after wake: refresh the access token and
        open pooled TLS connections so the first real tool call of the session skips both."""
        for scopes, url in _WARM_UP_PROBES:
            try:
                _sub, credentials = self.accounts.credentials_for(**scopes)
                warm_up_connection(credentials, url)
            except OAuthError:
                continue  # not connected / scope not granted — nothing to warm
            except Exception as exc:
                logger.info("google.warm_up skipped: %s", type(exc).__name__)
                return

    def calendar_action(self, **kwargs: Any) -> AgentResult:
        problem = calendar_call_problem(kwargs)
        result = problem if problem is not None else self.calendar.handle(**kwargs)
        logger.info(
            "calendar_action action=%s status=%s op_id=%s kind=%s",
            kwargs.get("action"),
            result.status,
            (result.data or {}).get("op_id"),
            (result.data or {}).get("kind"),
        )
        return result

    def gmail_action(self, **kwargs: Any) -> AgentResult:
        return self.gmail.handle(**kwargs)

    def notes_action(self, **kwargs: Any) -> AgentResult:
        return self.notes.handle(**kwargs)


def _asdict_safe(status: Any) -> dict[str, Any]:
    if hasattr(status, "__dataclass_fields__"):
        return asdict(status)
    return {}
