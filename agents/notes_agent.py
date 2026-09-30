"""Personal notes agent — Google Drive Doc as source of truth (v2 edit/delete)."""
from __future__ import annotations

import logging
from typing import Callable

from agents.types import AgentResult, result_from_google_error
from auth.account_manager import AccountManager
from auth.google_oauth import OAuthError
from integrations.google_errors import GoogleApiError
from integrations.google_notes import (
    FakeNotesClient,
    GoogleNotesClient,
    NotesAmbiguousError,
    NotesClient,
    NotesConflictError,
    NotesNotFoundError,
    NotesService,
)

logger = logging.getLogger(__name__)


class NotesAgent:
    def __init__(
        self,
        accounts: AccountManager,
        *,
        timezone: str = "Europe/Kyiv",
        client_factory: Callable[..., NotesClient] | None = None,
    ) -> None:
        self._accounts = accounts
        self._timezone = timezone
        self._client_factory = client_factory or (lambda creds: GoogleNotesClient(creds))
        self._services: dict[str, NotesService] = {}

    def _service_for_user(self) -> tuple[str, NotesService]:
        sub, creds = self._accounts.credentials_for(notes=True)
        service = self._services.get(sub)
        if service is None:
            service = NotesService(self._client_factory(creds), timezone=self._timezone)
            self._services[sub] = service
        return sub, service

    def clear_cache(self, google_sub: str | None = None) -> None:
        if google_sub:
            self._services.pop(google_sub, None)
        else:
            self._services.clear()

    def handle(
        self,
        action: str,
        *,
        content: str | None = None,
        title: str | None = None,
        category: str | None = None,
        query: str | None = None,
        target: str | None = None,
        note_id: str | None = None,
        append_text: str | None = None,
        limit: int | None = None,
        date_filter: str | None = None,
        date: str | None = None,
        user_sub: str | None = None,
        email: str | None = None,
        session_id: str | None = None,
        **_ignored: object,
    ) -> AgentResult:
        del user_sub, email, session_id
        action = (action or "").strip().lower()
        try:
            if action in ("add", "add_note", "create", "write", "remember"):
                return self.add_note(content or "", title=title, category=category)
            if action in ("read", "read_notes", "list", "list_notes", "latest"):
                return self.read_notes(
                    limit=limit if limit is not None else 10,
                    date_filter=date_filter,
                    date=date,
                )
            if action in ("search", "search_notes", "find"):
                return self.search_notes(
                    query or "",
                    limit=limit if limit is not None else 10,
                    date_filter=date_filter,
                    date=date,
                )
            if action in ("count", "notes_count"):
                return self.count_notes()
            if action in ("update", "edit", "replace"):
                return self.update_note(
                    note_id=note_id,
                    query=query,
                    target=target,
                    content=content,
                    title=title,
                    append_text=None,
                )
            if action in ("append", "add_to"):
                return self.update_note(
                    note_id=note_id,
                    query=query,
                    target=target,
                    append_text=append_text or content,
                    title=title,
                )
            if action in ("delete", "remove"):
                return self.delete_note(note_id=note_id, query=query, target=target)
            return AgentResult(
                "needs_more_info",
                "Доступні дії з нотатками: add, read, search, count, update, append, delete.",
            )
        except NotesAmbiguousError as exc:
            return AgentResult(
                "ambiguous",
                "Знайшов кілька схожих нотаток — уточни, яку саме.",
                {
                    "action": "notes_ambiguous",
                    "matches": [n.to_dict() for n in exc.matches],
                    "count": len(exc.matches),
                },
            )
        except NotesNotFoundError:
            return AgentResult(
                "not_found",
                "Не знайшов таку нотатку.",
                {"action": "notes_not_found"},
            )
        except NotesConflictError as exc:
            return result_from_google_error(exc)
        except OAuthError as exc:
            return result_from_google_error(exc)
        except GoogleApiError as exc:
            return result_from_google_error(exc)
        except ValueError as exc:
            code = str(exc)
            if code == "empty_content":
                return AgentResult(
                    "needs_more_info",
                    "Що саме записати в нотатку? Скажи зміст ще раз.",
                    {"action": "note_add", "reason": "empty_content"},
                )
            if code == "empty_append":
                return AgentResult(
                    "needs_more_info",
                    "Що саме дописати до нотатки?",
                    {"action": "notes_append", "reason": "empty_append"},
                )
            if code == "empty_update":
                return AgentResult(
                    "needs_more_info",
                    "Що змінити в нотатці — заголовок, текст чи дописати?",
                    {"action": "notes_update", "reason": "empty_update"},
                )
            return AgentResult("error", "Не вдалося обробити нотатку.")
        except Exception as exc:
            logger.exception("notes.unexpected_error type=%s", type(exc).__name__)
            return AgentResult("error", "Не вдалося виконати дію з нотатками.")

    def add_note(
        self,
        content: str,
        *,
        title: str | None = None,
        category: str | None = None,
    ) -> AgentResult:
        _sub, service = self._service_for_user()
        document_id, note = service.add_note(content, title=title, category=category)
        logger.info(
            "notes.added document_id=%s… note_id=%s",
            document_id[:8],
            note.note_id,
        )
        return AgentResult(
            "success",
            f"Записав нотатку «{note.title}».",
            {
                "success": True,
                "action": "note_added",
                "title": note.title,
                "content": note.content,
                "timestamp": note.timestamp,
                "note_id": note.note_id,
                "document_id": document_id,
            },
        )

    def read_notes(
        self,
        *,
        limit: int = 10,
        date_filter: str | None = None,
        date: str | None = None,
    ) -> AgentResult:
        _sub, service = self._service_for_user()
        document_id, notes, total = service.list_notes(
            limit=limit, date_filter=date_filter, date=date
        )
        if not notes:
            return AgentResult(
                "success",
                "Поки що немає збережених нотаток.",
                {
                    "success": True,
                    "action": "notes_read",
                    "notes": [],
                    "count": 0,
                    "total_count": total,
                    "document_id": document_id,
                },
            )
        lines = [f"{n.timestamp} — {n.title}: {n.preview(160)}" for n in notes]
        spoken = "Останні нотатки: " + " | ".join(lines)
        return AgentResult(
            "success",
            spoken,
            {
                "success": True,
                "action": "notes_read",
                "notes": [n.to_dict() for n in notes],
                "count": len(notes),
                "total_count": total,
                "document_id": document_id,
            },
        )

    def search_notes(
        self,
        query: str,
        *,
        limit: int = 10,
        date_filter: str | None = None,
        date: str | None = None,
    ) -> AgentResult:
        q = (query or "").strip()
        if not q:
            return AgentResult(
                "needs_more_info",
                "Що саме шукати в нотатках? Назви ключове слово.",
                {"action": "notes_search", "reason": "empty_query"},
            )
        _sub, service = self._service_for_user()
        document_id, notes = service.search(
            q, limit=limit, date_filter=date_filter, date=date
        )
        if not notes:
            return AgentResult(
                "not_found",
                f"Не знайшов нотаток за запитом «{q}».",
                {
                    "success": False,
                    "action": "notes_search",
                    "query": q,
                    "notes": [],
                    "count": 0,
                    "document_id": document_id,
                },
            )
        lines = [f"{n.timestamp} — {n.title}: {n.content}" for n in notes]
        spoken = f"Знайшов {len(notes)}: " + " | ".join(lines)
        return AgentResult(
            "success",
            spoken,
            {
                "success": True,
                "action": "notes_search",
                "query": q,
                "notes": [n.to_dict() for n in notes],
                "count": len(notes),
                "document_id": document_id,
            },
        )

    def count_notes(self) -> AgentResult:
        _sub, service = self._service_for_user()
        document_id, total = service.count_notes()
        if total == 0:
            msg = "У тебе ще немає нотаток."
        elif total == 1:
            msg = "У тебе 1 нотатка."
        elif 2 <= total <= 4:
            msg = f"У тебе {total} нотатки."
        else:
            msg = f"У тебе {total} нотаток."
        return AgentResult(
            "success",
            msg,
            {
                "success": True,
                "action": "notes_count",
                "total_count": total,
                "count": total,
                "document_id": document_id,
            },
        )

    def update_note(
        self,
        *,
        note_id: str | None = None,
        query: str | None = None,
        target: str | None = None,
        content: str | None = None,
        title: str | None = None,
        append_text: str | None = None,
    ) -> AgentResult:
        _sub, service = self._service_for_user()
        document_id, note = service.update_note(
            note_id=note_id,
            query=query,
            target=target,
            content=content,
            title=title,
            append_text=append_text,
        )
        if append_text is not None:
            action = "appended"
            spoken = f"Доповнив нотатку «{note.title}»."
        elif title is not None and content is None:
            action = "renamed"
            spoken = f"Перейменував нотатку на «{note.title}»."
        else:
            action = "updated"
            spoken = f"Змінив нотатку «{note.title}»."
        logger.info(
            "notes.%s document_id=%s… note_id=%s",
            action,
            document_id[:8],
            note.note_id,
        )
        return AgentResult(
            "success",
            spoken,
            {
                "success": True,
                "action": action,
                "note_id": note.note_id,
                "title": note.title,
                "content": note.content,
                "timestamp": note.timestamp,
                "document_id": document_id,
            },
        )

    def delete_note(
        self,
        *,
        note_id: str | None = None,
        query: str | None = None,
        target: str | None = None,
    ) -> AgentResult:
        _sub, service = self._service_for_user()
        document_id, note = service.delete_note(
            note_id=note_id, query=query, target=target
        )
        logger.info(
            "notes.deleted document_id=%s… note_id=%s",
            document_id[:8],
            note.note_id,
        )
        return AgentResult(
            "success",
            f"Видалив нотатку «{note.title}».",
            {
                "success": True,
                "action": "deleted",
                "note_id": note.note_id,
                "title": note.title,
                "document_id": document_id,
            },
        )


__all__ = ["NotesAgent", "FakeNotesClient"]
