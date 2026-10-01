"""Server side of a browser Live session.

Audio flows browser ↔ OpenAI over WebRTC. This bridge attaches to the same session over a
sideband WebSocket and does what LiveVoiceSession does for the desktop app on its primary
connection: run the delegated function calls (Google Calendar/Gmail/notes, web search…)
and hand the results back so the model can continue. No audio, playback or local barge-in
here — the browser's echo cancellation and the Live model handle that.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from tools.executor import ToolExecutionContext, ToolExecutor
from voice.delegation import extract_completed_function_call

logger = logging.getLogger(__name__)


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class SidebandToolBridge:
    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        session_id: str,
        executor: ToolExecutor,
        on_closed: Callable[[SidebandToolBridge], None] | None = None,
    ) -> None:
        self._client = client
        self.session_id = session_id
        self._executor = executor
        self._on_closed = on_closed
        self._connection: Any = None
        self._closed = asyncio.Event()
        self._tool_tasks: set[asyncio.Task] = set()
        self._pending_tool_calls: dict[str, set[str]] = {}
        self._user_turns: list[str] = []
        self._input_buf = ""
        self.end_requested = False

    async def run(self) -> None:
        try:
            async with self._client.live.sideband.connect(session_id=self.session_id) as connection:
                self._connection = connection
                logger.info("web.sideband.connected session_id=%s", self.session_id)
                async for event in connection:
                    await self._handle_event(event)
                    if _attr(event, "type") == "session.closed":
                        break
        except Exception:
            logger.exception("web.sideband.failed session_id=%s", self.session_id)
        finally:
            self._closed.set()
            self._connection = None
            for task in list(self._tool_tasks):
                task.cancel()
            logger.info("web.sideband.closed session_id=%s", self.session_id)
            if self._on_closed:
                self._on_closed(self)

    @property
    def is_open(self) -> bool:
        return self._connection is not None and not self._closed.is_set()

    async def say(self, text: str) -> None:
        """Have the assistant tell the user something (e.g. a Google login finished)."""
        if not self.is_open:
            return
        await self._connection.session.commentary.append(
            content=text, delegation_id=None, event_id=f"comment_{uuid.uuid4().hex[:8]}"
        )

    async def close(self) -> None:
        if self.is_open:
            try:
                await self._connection.session.close()
            except Exception:
                logger.debug("web.sideband close failed", exc_info=True)

    async def _handle_event(self, event: Any) -> None:
        etype = _attr(event, "type")
        if etype == "session.input_transcript.delta":
            self._input_buf += _attr(event, "delta") or ""
            return
        if etype == "session.output_transcript.delta" and self._input_buf.strip():
            # The model started answering → the user's turn is complete.
            self._user_turns = (self._user_turns + [self._input_buf.strip()])[-20:]
            self._input_buf = ""
            return
        if etype == "error":
            logger.error("web.live.error session_id=%s detail=%s", self.session_id, _attr(event, "error") or event)
            return
        if etype != "response.event":
            return
        inner = _attr(event, "event")
        itype = _attr(inner, "type")
        if itype == "response.created":
            response = _attr(inner, "response")
            rid = _attr(response, "id") or _attr(inner, "response_id")
            if rid:
                self._pending_tool_calls.setdefault(str(rid), set())
            return
        if itype != "response.output_item.done":
            return
        completed = extract_completed_function_call(event)
        if completed is None:
            return
        if completed.response_id:
            self._pending_tool_calls.setdefault(completed.response_id, set()).add(completed.call_id)
        logger.info(
            "web.tool.call session_id=%s call_id=%s tool_name=%s",
            self.session_id,
            completed.call_id,
            completed.name,
        )
        # Never block the event loop on a tool: Google calls run in worker threads.
        task = asyncio.create_task(
            self._execute_and_continue(
                name=completed.name,
                arguments=completed.arguments,
                call_id=completed.call_id,
                delegation_id=completed.delegation_id,
                response_id=completed.response_id,
            )
        )
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _execute_and_continue(
        self,
        *,
        name: str,
        arguments: Any,
        call_id: str,
        delegation_id: str | None,
        response_id: str | None,
    ) -> None:
        ctx = ToolExecutionContext(
            session_id=self.session_id,
            delegation_id=delegation_id,
            user_utterances=list(self._user_turns) + ([self._input_buf.strip()] if self._input_buf.strip() else [])
            or None,
        )
        try:
            result = await self._executor.execute(name, arguments, ctx)
        except Exception:
            logger.exception("web.tool.failed tool_name=%s", name)
            return
        if not self.is_open:
            return
        if name == "end_conversation" and result.ok:
            self.end_requested = True
        logger.info(
            "web.tool.result session_id=%s call_id=%s tool_name=%s status=%s",
            self.session_id,
            call_id,
            name,
            result.status,
        )
        await self._connection.response.item.create(
            event_id=f"tool_result_{call_id}",
            item={"type": "function_call_output", "call_id": call_id, "output": result.to_json()},
        )
        if response_id and response_id in self._pending_tool_calls:
            self._pending_tool_calls[response_id].discard(call_id)
            if self._pending_tool_calls[response_id]:
                return  # continue only once every call of this response has a result
        if self.is_open:
            await self._connection.response.create(event_id=f"continue_{call_id}")
