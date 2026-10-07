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
from voice.conversation import ConversationLog
from voice.delegation import RESPONSE_FINISHED_TYPES, DelegatedResponseTracker, extract_completed_function_call
from voice.options import VOICE_REQUEST_RE

logger = logging.getLogger(__name__)

# All results are in but the response's finish event hasn't come: continue anyway after this.
_CONTINUE_FALLBACK_S = 2.0
# A request ends when the transcript has been quiet this long («зміни голос на …» + «чоловічий»).
_UTTERANCE_PAUSE_S = 0.9


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
        max_duration_s: float | None = None,
        conversation: ConversationLog | None = None,
        on_voice_request: Callable[[str], bool] | None = None,
    ) -> None:
        self._client = client
        self.session_id = session_id
        self._executor = executor
        self._on_closed = on_closed
        self._max_duration_s = max_duration_s
        self._connection: Any = None
        self._closed = asyncio.Event()
        self._tool_tasks: set[asyncio.Task] = set()
        self._responses = DelegatedResponseTracker()
        self._user_turns: list[str] = []
        self._input_buf = ""
        self.end_requested = False
        self._conversation = conversation
        # Set by change_voice: close the session right away (no spoken confirmation) so the page
        # reconnects in the new voice — Live voices are fixed per session — and carries on.
        self.restart_for_voice = False
        # «зміни голос …» is acted on here: the Live model often says «Секунду» and never delegates
        # it. on_voice_request(utterance) → True when it switched the voice (then we restart).
        self._on_voice_request = on_voice_request
        self._voice_check: asyncio.Task | None = None
        # instructions.append lands mid-answer as an interruption (it is how a barge-in stops her),
        # so setting changes wait until she has finished speaking.
        self._last_output_at = 0.0
        self._pending_instruction: asyncio.Task | None = None

    async def run(self) -> None:
        expiry: asyncio.Task | None = None
        try:
            async with self._client.live.sideband.connect(session_id=self.session_id) as connection:
                self._connection = connection
                logger.info("web.sideband.connected session_id=%s", self.session_id)
                if self._max_duration_s:
                    expiry = asyncio.create_task(self._expire_after(self._max_duration_s))
                async for event in connection:
                    await self._handle_event(event)
                    if _attr(event, "type") == "session.closed":
                        break
        except Exception:
            logger.exception("web.sideband.failed session_id=%s", self.session_id)
        finally:
            self._closed.set()
            self._connection = None
            if expiry is not None:
                expiry.cancel()
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

    async def append_instruction(self, text: str, *, quiet_s: float = 3.0, max_wait_s: float = 120.0) -> None:
        """New speed/style asked by voice (set_voice_style), sent once she is quiet — an instruction
        that arrives mid-answer derails it. A newer setting replaces one still waiting. (The page
        does this itself for the settings panel: it hears her actual audio.)"""
        if not self.is_open or not text:
            return
        if self._pending_instruction is not None:
            self._pending_instruction.cancel()
        self._pending_instruction = asyncio.create_task(self._append_when_quiet(text, quiet_s, max_wait_s))

    async def _append_when_quiet(self, text: str, quiet_s: float, max_wait_s: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_s
        # The transcript runs ahead of the audio: quiet_s of no new text ≈ she has finished.
        while self.is_open and loop.time() - self._last_output_at < quiet_s and loop.time() < deadline:
            await asyncio.sleep(0.2)
        if not self.is_open:
            return
        self._pending_instruction = None
        logger.info("web.voice.delivery_applied session_id=%s", self.session_id)
        await self._connection.session.instructions.append(
            content=text, delegation_id=None, event_id=f"instr_{uuid.uuid4().hex[:8]}"
        )

    async def _voice_request_after_pause(self, utterance: str) -> None:
        await asyncio.sleep(_UTTERANCE_PAUSE_S)
        self._voice_check = None
        if not self.is_open or self._on_voice_request is None:
            return
        # The transcript buffer may have moved on (the model answered); use the latest text.
        text = self._input_buf if VOICE_REQUEST_RE.search(self._input_buf) else utterance
        if self._on_voice_request(text):
            logger.info("web.voice.restart session_id=%s by=transcript", self.session_id)
            self.restart_for_voice = False
            await self.close()

    async def _expire_after(self, seconds: float) -> None:
        """Cost cap: a call nobody ended (tab left open, browser crashed) is closed server-side."""
        await asyncio.sleep(seconds)
        logger.info("web.session.expired session_id=%s after_s=%s", self.session_id, int(seconds))
        await self.close()

    async def close(self) -> None:
        if self.is_open:
            try:
                await self._connection.session.close()
            except Exception:
                logger.debug("web.sideband close failed", exc_info=True)

    async def _handle_event(self, event: Any) -> None:
        etype = _attr(event, "type")
        if etype == "session.input_transcript.delta":
            delta = _attr(event, "delta") or ""
            self._input_buf += delta
            if self._conversation is not None:
                self._conversation.add("user", delta)
            if self._on_voice_request is not None and VOICE_REQUEST_RE.search(self._input_buf):
                if self._voice_check is not None:
                    self._voice_check.cancel()  # wait for the end of the request
                self._voice_check = asyncio.create_task(self._voice_request_after_pause(self._input_buf))
            return
        if etype == "session.output_transcript.delta":
            delta = _attr(event, "delta") or ""
            self._last_output_at = asyncio.get_running_loop().time()
            if self._conversation is not None:
                self._conversation.add("assistant", delta)
            if self._input_buf.strip():
                # The model started answering → the user's turn is complete.
                self._user_turns = (self._user_turns + [self._input_buf.strip()])[-20:]
                self._input_buf = ""
            return
        if etype == "session.instructions.appended":
            logger.info("web.instructions.appended session_id=%s by=%s", self.session_id, _attr(event, "client_event_id"))
            return
        if etype == "error":
            logger.error("web.live.error session_id=%s detail=%s", self.session_id, _attr(event, "error") or event)
            return
        if etype != "response.event":
            return
        inner = _attr(event, "event")
        itype = _attr(inner, "type")
        response = _attr(inner, "response")
        key = str(_attr(event, "delegation_id") or _attr(response, "id") or _attr(inner, "response_id") or "")
        if itype == "response.created":
            self._responses.response_started(key)
            return
        if itype in RESPONSE_FINISHED_TYPES:
            if self._responses.response_finished(key, ok=itype == "response.completed"):
                await self._continue(key, reason="response_finished")
            return
        if itype != "response.output_item.done":
            return
        completed = extract_completed_function_call(event)
        if completed is None:
            return
        self._responses.call_started(key, completed.call_id)
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
                key=key,
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
        key: str,
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
        if self.restart_for_voice:
            # Don't let this session say anything more: the next one, in the new voice, carries on.
            self.restart_for_voice = False
            logger.info("web.voice.restart session_id=%s", self.session_id)
            await self.close()
            return
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
        if self._responses.call_finished(key, call_id):
            await self._continue(key, reason="results_in")
        elif self._responses.results_complete(key):
            task = asyncio.create_task(self._continue_if_stuck(key))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)

    async def _continue_if_stuck(self, key: str) -> None:
        await asyncio.sleep(_CONTINUE_FALLBACK_S)
        if self._responses.results_complete(key):
            await self._continue(key, reason="fallback")

    async def _continue(self, key: str, *, reason: str) -> None:
        """Every call of the delegated response has its result: let the model go on."""
        self._responses.mark_continued(key)
        if self.is_open:
            logger.info("web.response.continue session_id=%s reason=%s", self.session_id, reason)
            await self._connection.response.create(event_id=f"continue_{uuid.uuid4().hex[:8]}")
