"""Central tool executor for GPT-Live Responses delegation (and reusable by tests)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agents.types import AgentResult
from tools.calendar_tools import CalendarToolWrappers
from tools.gmail_tools import GmailToolWrappers
from tools.notes_tools import NotesToolWrappers
from tools.results import ToolResult, agent_result_to_tool_result
from tools.task_context import TaskRevisionTracker

logger = logging.getLogger(__name__)

HandlerFn = Callable[[dict[str, Any], "ToolExecutionContext"], "ToolResult | AgentResult | str"]

_MUTATING_PREPARE = frozenset(
    {
        "calendar_prepare_create",
        "calendar_prepare_update",
        "calendar_prepare_delete",
        "gmail_prepare_send",
        "gmail_prepare_reply",
    }
)
_CONFIRM_TOOLS = frozenset(
    {
        "calendar_confirm_operation",
        "calendar_reject_operation",
        "gmail_confirm_send",
        "gmail_reject_send",
    }
)

# Built-in calendar/gmail/notes wrappers always use blocking Google HTTP clients.
_ALWAYS_THREADED_PREFIXES = ("calendar_", "gmail_", "notes_")


@dataclass
class _HandlerSpec:
    handler: HandlerFn
    run_in_thread: bool = False


@dataclass
class ToolExecutionContext:
    session_id: str | None = None
    delegation_id: str | None = None
    task_revision: int | None = None
    user_utterances: list[str] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    """Parse/validate args, route to handlers, serialize structured results."""

    def __init__(
        self,
        *,
        calendar: CalendarToolWrappers,
        gmail: GmailToolWrappers | None = None,
        notes: NotesToolWrappers | None = None,
        revisions: TaskRevisionTracker | None = None,
        handlers: dict[str, HandlerFn | _HandlerSpec] | None = None,
    ) -> None:
        self._calendar = calendar
        self._gmail = gmail
        self._notes = notes
        self._revisions = revisions or TaskRevisionTracker()
        self._handlers: dict[str, _HandlerSpec] = {}
        for name, handler in (handlers or {}).items():
            if isinstance(handler, _HandlerSpec):
                self._handlers[name] = handler
            else:
                self._handlers[name] = _HandlerSpec(handler, run_in_thread=False)

    @property
    def revisions(self) -> TaskRevisionTracker:
        return self._revisions

    def register(
        self,
        name: str,
        handler: HandlerFn,
        *,
        run_in_thread: bool = False,
    ) -> None:
        self._handlers[name] = _HandlerSpec(handler, run_in_thread=run_in_thread)

    def is_offloaded(self, name: str) -> bool:
        spec = self._handlers.get(name)
        if spec is not None:
            return spec.run_in_thread
        return name.startswith(_ALWAYS_THREADED_PREFIXES)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | str | None,
        context: ToolExecutionContext,
    ) -> ToolResult:
        parsed = self._parse_arguments(arguments)
        if parsed is None:
            return ToolResult(ok=False, status="error", message="Некоректні аргументи інструмента.")

        if name in _CONFIRM_TOOLS:
            op_id = str(parsed.get("op_id") or "").strip()
            if op_id and not self._revisions.is_op_current(op_id):
                logger.info(
                    "live.backend.function_result stale op_id=%s tool=%s session_id=%s delegation_id=%s",
                    op_id,
                    name,
                    context.session_id,
                    context.delegation_id,
                )
                return ToolResult(
                    ok=False,
                    status="stale",
                    message="Ця дія вже застаріла через новішу зміну запиту. Підготуй нову дію.",
                    op_id=op_id,
                )

        if context.user_utterances is not None:
            parsed = dict(parsed)
            parsed["user_utterances"] = context.user_utterances

        started = time.monotonic()
        logger.info(
            "live.tool.started tool_name=%s session_id=%s delegation_id=%s offloaded=%s",
            name,
            context.session_id,
            context.delegation_id,
            self.is_offloaded(name),
        )
        try:
            raw = await self._dispatch(name, parsed, context)
        except TypeError as exc:
            logger.warning(
                "live.backend.function_result tool=%s error=TypeError session_id=%s",
                name,
                context.session_id,
            )
            return ToolResult(
                ok=False,
                status="error",
                message="Не вистачає параметрів команди. Повтори, що саме зробити.",
                data={"error_type": type(exc).__name__},
            )
        except Exception as exc:
            logger.exception(
                "live.backend.function_result tool=%s unexpected error session_id=%s",
                name,
                context.session_id,
            )
            return ToolResult(
                ok=False,
                status="error",
                message="Не вдалося виконати запит.",
                data={"error_type": type(exc).__name__},
            )

        result = self._normalize(raw)
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "live.tool.completed tool_name=%s session_id=%s status=%s duration_ms=%s",
            name,
            context.session_id,
            result.status,
            duration_ms,
        )
        # Bump revision only after a successful new pending prepare so a failed
        # prepare (busy/PendingConflict) cannot stale the still-valid op_id.
        if name in _MUTATING_PREPARE and result.status == "confirmation_required" and result.op_id:
            rev = self._revisions.bump(reason=name)
            context.task_revision = rev
            self._revisions.bind_op(result.op_id, rev)
        if context.delegation_id and context.task_revision is not None:
            self._revisions.bind_delegation(context.delegation_id, context.task_revision)
        return result

    def execute_sync(
        self,
        name: str,
        arguments: dict[str, Any] | str | None,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Sync entry for unit tests / sync callers."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError("execute_sync cannot be called from a running event loop")
        return asyncio.run(self.execute(name, arguments, context))

    async def _dispatch(
        self,
        name: str,
        args: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult | AgentResult | str:
        if name in self._handlers:
            spec = self._handlers[name]
            return await self._call_handler(name, spec, args, context)

        sid = context.session_id

        def _calendar_call() -> AgentResult:
            if name == "calendar_list_events":
                return self._calendar.list_events(args, session_id=sid)
            if name == "calendar_search_events":
                return self._calendar.search_events(args, session_id=sid)
            if name == "calendar_prepare_create":
                return self._calendar.prepare_create(args, session_id=sid)
            if name == "calendar_prepare_update":
                return self._calendar.prepare_update(args, session_id=sid)
            if name == "calendar_prepare_delete":
                return self._calendar.prepare_delete(args, session_id=sid)
            if name == "calendar_confirm_operation":
                return self._calendar.confirm(args, session_id=sid)
            if name == "calendar_reject_operation":
                return self._calendar.reject(args, session_id=sid)
            raise KeyError(name)

        if name.startswith("calendar_"):
            logger.info("live.tool.offloaded_to_thread tool_name=%s", name)
            return await asyncio.to_thread(_calendar_call)

        if name.startswith("gmail_"):
            if self._gmail is None:
                return ToolResult(ok=False, status="error", message="Gmail tools are not configured.")

            def _gmail_call() -> AgentResult:
                if name == "gmail_search_messages":
                    return self._gmail.search_messages(args, session_id=sid)
                if name == "gmail_read_message":
                    return self._gmail.read_message(args, session_id=sid)
                if name == "gmail_create_draft":
                    return self._gmail.create_draft(args, session_id=sid)
                if name == "gmail_prepare_send":
                    return self._gmail.prepare_send(args, session_id=sid)
                if name == "gmail_prepare_reply":
                    return self._gmail.prepare_reply(args, session_id=sid)
                if name == "gmail_confirm_send":
                    return self._gmail.confirm_send(args, session_id=sid)
                if name == "gmail_reject_send":
                    return self._gmail.reject_send(args, session_id=sid)
                raise KeyError(name)

            logger.info("live.tool.offloaded_to_thread tool_name=%s", name)
            try:
                return await asyncio.to_thread(_gmail_call)
            except KeyError:
                return ToolResult(ok=False, status="error", message=f"Невідома команда: {name}")

        if name.startswith("notes_"):
            if self._notes is None:
                return ToolResult(ok=False, status="error", message="Notes tools are not configured.")

            def _notes_call() -> AgentResult:
                if name == "notes_add":
                    return self._notes.add_note(args, session_id=sid)
                if name == "notes_read":
                    return self._notes.read_notes(args, session_id=sid)
                if name == "notes_search":
                    return self._notes.search_notes(args, session_id=sid)
                if name == "notes_count":
                    return self._notes.count_notes(args, session_id=sid)
                if name == "notes_update":
                    return self._notes.update_note(args, session_id=sid)
                if name == "notes_append":
                    return self._notes.append_note(args, session_id=sid)
                if name == "notes_delete":
                    return self._notes.delete_note(args, session_id=sid)
                raise KeyError(name)

            logger.info("live.tool.offloaded_to_thread tool_name=%s", name)
            try:
                return await asyncio.to_thread(_notes_call)
            except KeyError:
                return ToolResult(ok=False, status="error", message=f"Невідома команда: {name}")

        return ToolResult(ok=False, status="error", message=f"Невідома команда: {name}")

    async def _call_handler(
        self,
        name: str,
        spec: _HandlerSpec,
        args: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult | AgentResult | str:
        if spec.run_in_thread:
            logger.info("live.tool.offloaded_to_thread tool_name=%s", name)
            return await asyncio.to_thread(spec.handler, args, context)
        return spec.handler(args, context)

    @staticmethod
    def _parse_arguments(arguments: dict[str, Any] | str | None) -> dict[str, Any] | None:
        if arguments is None:
            return {}
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            text = arguments.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, dict):
                return None
            return parsed
        return None

    @staticmethod
    def _normalize(raw: ToolResult | AgentResult | str) -> ToolResult:
        if isinstance(raw, ToolResult):
            return raw
        if isinstance(raw, AgentResult):
            return agent_result_to_tool_result(raw)
        return ToolResult(ok=True, status="ok", message=str(raw))
