"""Gmail agent — search/read/draft/send with explicit human confirmation before send."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Callable

from agents.pending_store import PendingConflict, PendingStore
from agents.types import AgentResult, result_from_google_error
from auth.account_manager import AccountManager
from auth.google_oauth import OAuthError
from integrations.google_errors import GoogleApiError
from integrations.google_gmail import GmailClient, GoogleGmailClient, sanitize_email_text

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fingerprint(to: str, subject: str, body: str) -> str:
    raw = f"{to}\n{subject}\n{body}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class GmailAgent:
    def __init__(
        self,
        accounts: AccountManager,
        pending: PendingStore,
        client_factory: Callable[..., GmailClient] | None = None,
    ) -> None:
        self._accounts = accounts
        self._pending = pending
        self._client_factory = client_factory or (lambda creds: GoogleGmailClient(creds))

    def _pending_busy(self, sub: str) -> AgentResult:
        op = self._pending.get(sub)
        if op is None:
            return AgentResult("error", "Попередня дія ще виконується. Зачекай результат.")
        return AgentResult(
            "needs_more_info" if op.state == "pending" else "error",
            f"Зараз очікую завершення дії: {op.summary_uk} Спочатку «так» або «ні», нову дію не підміняю.",
            {"op_id": op.op_id, "pending_kind": op.kind, "pending_state": op.state},
        )

    def _client_for_user(
        self,
        *,
        readonly: bool = False,
        compose: bool = False,
        send: bool = False,
        full: bool = False,
    ) -> tuple[str, GmailClient]:
        if full:
            sub, creds = self._accounts.credentials_for(gmail=True)
        else:
            sub, creds = self._accounts.credentials_for(
                gmail_readonly=readonly,
                gmail_compose=compose,
                gmail_send=send,
            )
        return sub, self._client_factory(creds)

    def handle(
        self,
        action: str,
        *,
        to: str | None = None,
        subject: str | None = None,
        body: str | None = None,
        query: str | None = None,
        message_id: str | None = None,
        draft_id: str | None = None,
        confirmation: str | None = None,
        user_sub: str | None = None,
        **_ignored: object,
    ) -> AgentResult:
        del user_sub
        action = (action or "").strip().lower()
        confirmation = (confirmation or "").strip().lower() or None

        if action in ("confirm", "reject") or confirmation in (
            "yes", "no", "так", "ні", "нет", "confirm", "cancel", "підтверджую", "скасуй", "не треба"
        ):
            return self._handle_confirmation(confirmation or action)

        try:
            if action == "search":
                return self.search(query or "")
            if action in ("read", "get", "view"):
                return self.read(message_id or "")
            if action == "draft":
                return self.create_draft(to=to, subject=subject, body=body)
            if action == "send":
                return self.propose_send(to=to, subject=subject, body=body, draft_id=draft_id)
            return AgentResult(
                "needs_more_info",
                "Яка дія з поштою: пошук, перегляд, чернетка чи надсилання?",
            )
        except (OAuthError, GoogleApiError) as exc:
            return result_from_google_error(exc)

    def search(self, query: str) -> AgentResult:
        if not query.strip():
            return AgentResult("needs_more_info", "Що шукати в пошті?")
        _sub, client = self._client_for_user(readonly=True)
        messages = client.search(query.strip(), max_results=5)
        if not messages:
            return AgentResult("success", "Листів за цим запитом не знайдено.", {"messages": []})
        lines = []
        data = []
        for msg in messages:
            lines.append(f"від {msg.get('from', '?')}: {msg.get('subject', '(без теми)')}")
            data.append({"message_id": msg.get("id"), "subject": msg.get("subject"), "from": msg.get("from")})
        return AgentResult("success", "Знайдені листи: " + "; ".join(lines), {"messages": data})

    def read(self, message_id: str) -> AgentResult:
        if not message_id.strip():
            return AgentResult("needs_more_info", "Вкажи ідентифікатор листа для перегляду.")
        _sub, client = self._client_for_user(readonly=True)
        msg = client.get_message(message_id.strip())
        body = sanitize_email_text(msg.get("body") or msg.get("snippet") or "")
        spoken = (
            f"Лист від {msg.get('from', '?')}, тема: {msg.get('subject', '(без теми)')}. "
            f"Коротко: {msg.get('snippet', '')[:200]}"
        )
        return AgentResult(
            "success",
            spoken,
            {
                "message_id": msg.get("id"),
                "subject": msg.get("subject"),
                "from": msg.get("from"),
                "body_untrusted": body,
            },
        )

    def create_draft(self, *, to: str | None, subject: str | None, body: str | None) -> AgentResult:
        missing = []
        if not to or not _EMAIL_RE.match(to.strip()):
            missing.append("email одержувача")
        if not (subject or "").strip():
            missing.append("тему")
        if not (body or "").strip():
            missing.append("текст")
        if missing:
            return AgentResult("needs_more_info", "Для чернетки потрібні: " + ", ".join(missing) + ".")
        _sub, client = self._client_for_user(compose=True)
        draft = client.create_draft(to.strip(), subject.strip(), body.strip())
        return AgentResult(
            "success",
            f"Чернетку збережено для {to}. Щоб надіслати — підтверди окремо.",
            {**draft},
        )

    def propose_send(
        self,
        *,
        to: str | None,
        subject: str | None,
        body: str | None,
        draft_id: str | None,
    ) -> AgentResult:
        # Propose needs compose (draft reload) or send capability foreshadowed.
        sub, client = self._client_for_user(compose=True, send=True, readonly=bool(draft_id))

        if draft_id:
            # Always reload draft from Google before asking confirmation.
            draft = client.get_draft(draft_id.strip())
            to_v, subject_v, body_v = draft["to"], draft["subject"], draft["body"]
            fp = _fingerprint(to_v, subject_v, body_v)
            summary = (
                f"Надіслати чернетку на {to_v}, тема «{subject_v}». "
                f"Текст: {body_v[:120]}{'…' if len(body_v) > 120 else ''}. Підтвердити?"
            )
            try:
                op = self._pending.put(
                    sub,
                    "gmail_send",
                    summary,
                    {
                        "draft_id": draft_id.strip(),
                        "to": to_v,
                        "subject": subject_v,
                        "body": body_v,
                        "content_fingerprint": fp,
                    },
                )
            except PendingConflict:
                return self._pending_busy(sub)
            return AgentResult("confirmation_required", summary, {"op_id": op.op_id, "fingerprint": fp})

        missing = []
        if not to or not _EMAIL_RE.match(to.strip()):
            missing.append("email одержувача")
        if not (subject or "").strip():
            missing.append("тему")
        if not (body or "").strip():
            missing.append("текст листа")
        if missing:
            return AgentResult("needs_more_info", "Щоб надіслати лист, потрібні: " + ", ".join(missing) + ".")

        to_v, subject_v, body_v = to.strip(), subject.strip(), body.strip()
        fp = _fingerprint(to_v, subject_v, body_v)
        summary = f"Надіслати лист на {to_v} з темою «{subject_v}». Підтвердити?"
        try:
            op = self._pending.put(
                sub,
                "gmail_send",
                summary,
                {"to": to_v, "subject": subject_v, "body": body_v, "content_fingerprint": fp},
            )
        except PendingConflict:
            return self._pending_busy(sub)
        return AgentResult("confirmation_required", summary, {"op_id": op.op_id})

    def _handle_confirmation(self, confirmation: str) -> AgentResult:
        try:
            sub = self._accounts.require_active_sub()
        except OAuthError as exc:
            return result_from_google_error(exc)

        yes = confirmation in ("yes", "так", "confirm", "підтверджую", "да")
        no = confirmation in ("no", "ні", "нет", "cancel", "reject", "скасуй", "не треба")
        if not yes and not no:
            return AgentResult("needs_more_info", "Потрібна чітка відповідь «так» або «ні» перед надсиланням.")

        op = self._pending.get(sub)
        if op is None or op.kind != "gmail_send":
            return AgentResult("error", "Немає листа, що очікує на підтвердження надсилання.")
        if op.user_sub != sub:
            self._pending.clear(sub)
            return AgentResult("error", "Підтвердження не відповідає активному акаунту.")

        if op.state == "completed" and op.execution_result:
            return AgentResult("success", op.execution_result.get("message", "Лист уже надіслано раніше."), op.execution_result)
        if op.state == "ambiguous":
            return AgentResult(
                "error",
                op.ambiguous_reason
                or "Надсилання могло вже відбутись — перевір Sent і не повторюй «так».",
            )

        if no:
            self._pending.mark_cancelled(sub)
            return AgentResult("success", "Добре, лист не надсилаю.")

        claimed = self._pending.begin_execute(sub, op.op_id)
        if claimed is None:
            again = self._pending.get(sub)
            if again and again.state == "completed" and again.execution_result:
                return AgentResult("success", again.execution_result.get("message", "Лист уже надіслано раніше."), again.execution_result)
            if again and again.state == "executing":
                return AgentResult("error", "Надсилання вже виконується — зачекай.")
            return AgentResult("error", "Не вдалося підтвердити надсилання.")

        if claimed.state == "completed" and claimed.execution_result:
            return AgentResult("success", claimed.execution_result.get("message", "Лист уже надіслано раніше."), claimed.execution_result)

        try:
            _, client = self._client_for_user(send=True, compose=True, readonly=True)
            draft_id = claimed.payload.get("draft_id")
            if draft_id:
                live = client.get_draft(draft_id)
                live_fp = _fingerprint(live["to"], live["subject"], live["body"])
                if live_fp != claimed.payload.get("content_fingerprint"):
                    self._pending.clear(sub)
                    return AgentResult(
                        "needs_more_info",
                        "Чернетка змінилась після попереднього перегляду. "
                        f"Зараз: на {live['to']}, тема «{live['subject']}». "
                        "Запроси надсилання знову для нового підтвердження.",
                    )
                sent = client.send_draft(draft_id)
            else:
                sent = client.send_message(
                    claimed.payload["to"], claimed.payload["subject"], claimed.payload["body"]
                )
            if not sent.get("message_id"):
                self._pending.mark_ambiguous(
                    sub,
                    "Google не повернув id листа — можливо вже надіслано. Перевір Sent, не повторюй.",
                )
                return AgentResult("error", "Google не підтвердив надсилання листа. Перевір Sent.")
            message = "Лист надіслано."
            result = AgentResult("success", message, sent)
            self._pending.mark_executed(sub, sent | {"message": message})
            return result
        except GoogleApiError as exc:
            if exc.code in ("network", "timeout", "google_unavailable") or (
                exc.http_status is not None and exc.http_status >= 500
            ):
                self._pending.mark_ambiguous(
                    sub,
                    "Зв'язок обірвався під час надсилання. Лист міг уже піти — перевір Sent, не повторюй «так».",
                )
                return AgentResult("error", self._pending.get(sub).ambiguous_reason if self._pending.get(sub) else str(exc))
            self._pending.clear(sub)
            return result_from_google_error(exc)
        except OAuthError as exc:
            self._pending.clear(sub)
            return result_from_google_error(exc)
