"""Central tool executor for GPT-Live Responses delegation (and reusable by tests)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from agents.types import AgentResult
from tools.calendar_tools import CalendarToolWrappers
from tools.results import ToolResult, agent_result_to_tool_result
from tools.task_context import TaskRevisionTracker

logger = logging.getLogger(__name__)

_MUTATING_PREPARE = frozenset(
    {
        "calendar_prepare_create",
        "calendar_prepare_update",
        "calendar_prepare_delete",
    }
)
_CONFIRM_TOOLS = frozenset({"calendar_confirm_operation", "calendar_reject_operation"})


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
        revisions: TaskRevisionTracker | None = None,
        handlers: dict[str, Callable[[dict[str, Any], ToolExecutionContext], ToolResult | AgentResult | str]]
        | None = None,
    ) -> None:
        self._calendar = calendar
        self._revisions = revisions or TaskRevisionTracker()
        self._handlers = handlers or {}

    @property
    def revisions(self) -> TaskRevisionTracker:
        return self._revisions

    def register(
        self,
        name: str,
        handler: Callable[[dict[str, Any], ToolExecutionContext], ToolResult | AgentResult | str],
    ) -> None:
        self._handlers[name] = handler

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
        import asyncio

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
            return self._handlers[name](args, context)

        sid = context.session_id
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

        return ToolResult(ok=False, status="error", message=f"Невідома команда: {name}")

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
