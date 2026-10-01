"""Gmail agent — search/read/draft/send/reply with explicit human confirmation before send."""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable

from agents.pending_store import PendingConflict, PendingStore
from agents.types import AgentResult, result_from_google_error
from auth.account_manager import AccountManager
from auth.google_oauth import OAuthError
from integrations.google_errors import GoogleApiError
from integrations.google_gmail import (
    GmailClient,
    GoogleGmailClient,
    extract_email_address,
    reply_subject,
    sanitize_email_text,
)

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GMAIL_PENDING_KINDS = frozenset({"gmail_send", "gmail_reply"})


def _fingerprint(to: str, subject: str, body: str) -> str:
    raw = f"{to}\n{subject}\n{body}".encode()
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

    def _own_email(self) -> str | None:
        try:
            status = self._accounts.status()
        except Exception:
            return None
        email = (status.email or "").strip().lower()
        return email or None

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
        op_id: str | None = None,
        session_id: str | None = None,
        user_sub: str | None = None,
        email: str | None = None,
        **_ignored: object,
    ) -> AgentResult:
        # Never trust model-supplied identity.
        del user_sub, email
        action = (action or "").strip().lower()
        confirmation = (confirmation or "").strip().lower() or None

        if action in ("confirm", "reject") or confirmation in (
            "yes",
            "no",
            "так",
            "ні",
            "нет",
            "confirm",
            "cancel",
            "підтверджую",
            "скасуй",
            "не треба",
        ):
            conf = confirmation or ("no" if action == "reject" else action)
            return self._handle_confirmation(conf, op_id=op_id, session_id=session_id)

        try:
            if action == "search":
                return self.search(query or "")
            if action in ("read", "get", "view"):
                return self.read(message_id or "")
            if action == "draft":
                return self.create_draft(to=to, subject=subject, body=body)
            if action == "send":
                return self.propose_send(
                    to=to,
                    subject=subject,
                    body=body,
                    draft_id=draft_id,
                    session_id=session_id,
                )
            if action in ("reply", "prepare_reply"):
                return self.propose_reply(
                    message_id=message_id or "",
                    body=body or "",
                    session_id=session_id,
                )
            return AgentResult(
                "needs_more_info",
                "Яка дія з поштою: пошук, перегляд, чернетка, надсилання чи відповідь?",
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
            data.append(
                {
                    "message_id": msg.get("id"),
                    "subject": msg.get("subject"),
                    "from": msg.get("from"),
                    "snippet": msg.get("snippet", ""),
                    "date": msg.get("date", ""),
                    "format": msg.get("format", "metadata"),
                }
            )
        return AgentResult("success", "Знайдені листи: " + "; ".join(lines), {"messages": data})

    def read(self, message_id: str) -> AgentResult:
        if not message_id.strip():
            return AgentResult("needs_more_info", "Вкажи ідентифікатор листа для перегляду.")
        _sub, client = self._client_for_user(readonly=True)
        msg = client.get_message(message_id.strip())
        body = sanitize_email_text(msg.get("body_raw") or msg.get("snippet") or "")
        # Prefer already-sanitized body from client if present.
        if msg.get("body") and "НЕДОВІРЕНИЙ" in str(msg.get("body")):
            body = msg["body"]
        spoken = (
            f"Лист від {msg.get('from', '?')}, тема: {msg.get('subject', '(без теми)')}. "
            f"Коротко: {msg.get('snippet', '')[:200]}"
        )
        return AgentResult(
            "success",
            spoken,
            {
                "message_id": msg.get("id"),
                "thread_id": msg.get("thread_id"),
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
        session_id: str | None = None,
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
                    session_id=session_id,
                )
            except PendingConflict:
                return self._pending_busy(sub)
            return AgentResult(
                "confirmation_required",
                summary,
                {"op_id": op.op_id, "fingerprint": fp, "kind": op.kind},
            )

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
                {
                    "to": to_v,
                    "subject": subject_v,
                    "body": body_v,
                    "content_fingerprint": fp,
                },
                session_id=session_id,
            )
        except PendingConflict:
            return self._pending_busy(sub)
        return AgentResult(
            "confirmation_required",
            summary,
            {"op_id": op.op_id, "kind": op.kind},
        )

    def propose_reply(
        self,
        *,
        message_id: str,
        body: str,
        session_id: str | None = None,
    ) -> AgentResult:
        if not (message_id or "").strip():
            return AgentResult("needs_more_info", "Вкажи лист, на який відповідати.")
        if not (body or "").strip():
            return AgentResult("needs_more_info", "Який текст відповіді надіслати?")

        sub, client = self._client_for_user(readonly=True, send=True)
        original = client.get_message(message_id.strip())

        # Recipient from headers only — never from untrusted body text.
        reply_to = extract_email_address(original.get("reply_to"))
        sender = extract_email_address(original.get("from"))
        to_v = reply_to or sender
        own = self._own_email()
        if not to_v:
            return AgentResult(
                "needs_more_info",
                "Не вдалося визначити адресу для відповіді з заголовків листа.",
            )
        if own and to_v.lower() == own:
            return AgentResult(
                "needs_more_info",
                "Не можу відповісти самому собі — обери інший лист або вкажи одержувача для нового листа.",
            )

        subject_v = reply_subject(original.get("subject"))
        body_v = body.strip()
        thread_id = original.get("thread_id") or ""
        if not thread_id:
            return AgentResult("error", "У листа немає threadId — відповідь у треді неможлива.")

        in_reply_to = (original.get("message_id_header") or "").strip() or None
        prior_refs = (original.get("references") or "").strip()
        if in_reply_to and prior_refs:
            references = f"{prior_refs} {in_reply_to}".strip()
        else:
            references = prior_refs or in_reply_to

        fp = _fingerprint(to_v, subject_v, body_v)
        summary = (
            f"Відповісти на лист від {original.get('from', '?')} "
            f"(тема «{subject_v}») на {to_v}. Підтвердити?"
        )
        try:
            op = self._pending.put(
                sub,
                "gmail_reply",
                summary,
                {
                    "to": to_v,
                    "subject": subject_v,
                    "body": body_v,
                    "content_fingerprint": fp,
                    "thread_id": thread_id,
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "source_message_id": message_id.strip(),
                },
                session_id=session_id,
            )
        except PendingConflict:
            return self._pending_busy(sub)
        return AgentResult(
            "confirmation_required",
            summary,
            {
                "op_id": op.op_id,
                "kind": op.kind,
                "to": to_v,
                "thread_id": thread_id,
            },
        )

    def _handle_confirmation(
        self,
        confirmation: str,
        *,
        op_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentResult:
        try:
            sub = self._accounts.require_active_sub()
        except OAuthError as exc:
            return result_from_google_error(exc)

        yes = confirmation in ("yes", "так", "confirm", "підтверджую", "да")
        no = confirmation in ("no", "ні", "нет", "cancel", "reject", "скасуй", "не треба")
        if not yes and not no:
            return AgentResult(
                "needs_more_info",
                "Потрібна чітка відповідь «так» або «ні» перед надсиланням.",
            )

        op, pending_reason = self._pending.get_with_reason(sub)
        if op is None:
            if pending_reason == "pending_expired":
                return AgentResult(
                    "error",
                    "Час підтвердження вичерпано. Запроси надсилання ще раз.",
                    {"reason_code": "pending_expired"},
                )
            return AgentResult(
                "error",
                "Немає листа, що очікує на підтвердження надсилання.",
                {"reason_code": "pending_missing"},
            )

        if op.kind not in _GMAIL_PENDING_KINDS:
            return AgentResult(
                "error",
                "Це підтвердження не для поштової дії.",
                {"op_id": op.op_id, "pending_kind": op.kind, "reason_code": "wrong_kind"},
            )

        if op.user_sub != sub:
            self._pending.clear(sub)
            return AgentResult(
                "error",
                "Підтвердження не відповідає активному акаунту.",
                {"reason_code": "account_mismatch"},
            )

        # Session binding: when both sides have a session, they must match.
        if session_id and op.session_id and session_id != op.session_id:
            return AgentResult(
                "error",
                "Підтвердження належить іншій сесії.",
                {
                    "op_id": op.op_id,
                    "reason_code": "session_mismatch",
                    "pending_state": op.state,
                },
            )

        # Exact op_id when provided (Live path). Legacy Realtime may omit op_id.
        if op_id and op_id != op.op_id:
            return AgentResult(
                "error",
                "Це підтвердження не відповідає очікуваній дії. Скажи ще раз так або ні.",
                {
                    "op_id": op.op_id,
                    "reason_code": "op_id_mismatch",
                    "pending_state": op.state,
                },
            )

        if op.state == "completed" and op.execution_result:
            return AgentResult(
                "success",
                op.execution_result.get("message", "Лист уже надіслано раніше."),
                op.execution_result,
            )
        if op.state == "ambiguous":
            return AgentResult(
                "error",
                op.ambiguous_reason
                or "Надсилання могло вже відбутись — перевір Sent і не повторюй «так».",
                {"op_id": op.op_id, "pending_state": "ambiguous", "kind": op.kind},
            )

        if no:
            self._pending.mark_cancelled(sub)
            return AgentResult(
                "success",
                "Добре, лист не надсилаю.",
                {"op_id": op.op_id, "kind": op.kind},
            )

        claimed = self._pending.begin_execute(sub, op.op_id)
        if claimed is None:
            again = self._pending.get(sub)
            if again and again.state == "completed" and again.execution_result:
                return AgentResult(
                    "success",
                    again.execution_result.get("message", "Лист уже надіслано раніше."),
                    again.execution_result,
                )
            if again and again.state == "executing":
                return AgentResult("error", "Надсилання вже виконується — зачекай.")
            return AgentResult("error", "Не вдалося підтвердити надсилання.")

        if claimed.state == "completed" and claimed.execution_result:
            return AgentResult(
                "success",
                claimed.execution_result.get("message", "Лист уже надіслано раніше."),
                claimed.execution_result,
            )

        try:
            return self._execute_send(sub, claimed)
        except GoogleApiError as exc:
            if exc.code in ("network", "timeout", "google_unavailable") or (
                exc.http_status is not None and exc.http_status >= 500
            ):
                self._pending.mark_ambiguous(
                    sub,
                    "Зв'язок обірвався під час надсилання. Лист міг уже піти — перевір Sent, не повторюй «так».",
                )
                amb = self._pending.get(sub)
                return AgentResult(
                    "error",
                    amb.ambiguous_reason if amb and amb.ambiguous_reason else str(exc),
                    {"op_id": claimed.op_id, "pending_state": "ambiguous"},
                )
            self._pending.clear(sub)
            return result_from_google_error(exc)
        except OAuthError as exc:
            self._pending.clear(sub)
            return result_from_google_error(exc)

    def _execute_send(self, sub: str, claimed) -> AgentResult:
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
        elif claimed.kind == "gmail_reply":
            sent = client.send_reply(
                to=claimed.payload["to"],
                subject=claimed.payload["subject"],
                body=claimed.payload["body"],
                thread_id=claimed.payload["thread_id"],
                in_reply_to=claimed.payload.get("in_reply_to"),
                references=claimed.payload.get("references"),
            )
        else:
            sent = client.send_message(
                claimed.payload["to"],
                claimed.payload["subject"],
                claimed.payload["body"],
            )
        if not sent.get("message_id"):
            self._pending.mark_ambiguous(
                sub,
                "Google не повернув id листа — можливо вже надіслано. Перевір Sent, не повторюй.",
            )
            return AgentResult(
                "error",
                "Google не підтвердив надсилання листа. Перевір Sent.",
                {"op_id": claimed.op_id, "pending_state": "ambiguous"},
            )
        message = "Лист надіслано." if claimed.kind == "gmail_send" else "Відповідь надіслано."
        result_data = dict(sent) | {"message": message, "op_id": claimed.op_id, "kind": claimed.kind}
        self._pending.mark_executed(sub, result_data)
        return AgentResult("success", message, result_data)
