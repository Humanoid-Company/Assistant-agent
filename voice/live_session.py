"""GPT-Live voice session with Responses delegation (calendar + Gmail tools)."""
from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional

from openai import AsyncOpenAI

from config import (
    OPENAI_API_KEY,
    OPENAI_LIVE_AUDIO_RATE,
    OPENAI_LIVE_BACKEND_MODEL,
    OPENAI_LIVE_MODEL,
    OPENAI_LIVE_VOICE,
)
from tools.executor import ToolExecutionContext, ToolExecutor
from voice.delegation import extract_completed_function_call
from voice.playback import PlaybackTracker

logger = logging.getLogger(__name__)

_BARGE_IN_COOLDOWN_S = 0.75
_SHUTDOWN_WAIT_S = 3.0


def _event_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class LiveVoiceSession:
    """Full-duplex GPT-Live session. Realtime VAD/response.create turn hacks are not used."""

    def __init__(
        self,
        *,
        tool_executor: ToolExecutor,
        voice: str | None = None,
        live_model: str | None = None,
        backend_model: str | None = None,
        audio_rate: int | None = None,
        on_user_transcript: Callable[[str], None] | None = None,
        session_id: str | None = None,
    ) -> None:
        self._executor = tool_executor
        self._voice = voice or OPENAI_LIVE_VOICE
        self._live_model = live_model or OPENAI_LIVE_MODEL
        self._backend_model = backend_model or OPENAI_LIVE_BACKEND_MODEL
        self._audio_rate = audio_rate or OPENAI_LIVE_AUDIO_RATE
        self._on_user_transcript = on_user_transcript
        self.session_id = session_id or str(uuid.uuid4())

        self.player = PlaybackTracker(sample_rate=self._audio_rate)
        self._turns: list[dict] = []
        self._input_buf = ""
        self._output_buf = ""

        self._sleep_requested = False
        self._voice_restart_requested = False
        self._closed = threading.Event()
        self._started = threading.Event()
        self._error: BaseException | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connection: Any = None
        self._client: AsyncOpenAI | None = None
        self._mic_read: Callable[[], bytes] | None = None
        self._live_instructions = ""
        self._backend_instructions = ""
        self._tasks: set[asyncio.Task] = set()
        self._tool_tasks: set[asyncio.Task] = set()
        self._pending_tool_calls: dict[str, set[str]] = {}  # response_id -> call_ids awaiting output
        self._delegation_ids: dict[str, str] = {}  # response_id -> delegation_id
        self._last_barge_in_at = 0.0
        self._close_sent = False
        self._session_closing = False

    @property
    def sleep_requested(self) -> bool:
        return self._sleep_requested

    @property
    def voice_restart_requested(self) -> bool:
        return self._voice_restart_requested

    def request_sleep(self) -> None:
        self._sleep_requested = True

    def request_voice_restart(self) -> None:
        self._voice_restart_requested = True
        self._sleep_requested = True

    def get_turns(self) -> list[dict]:
        return list(self._turns)

    def connect(
        self,
        instructions: str,
        *,
        mic_read_chunk: Callable[[], bytes],
        backend_instructions: str | None = None,
    ) -> None:
        if self._thread is not None:
            raise RuntimeError("LiveVoiceSession already connected")
        self._live_instructions = instructions
        self._backend_instructions = backend_instructions or ""
        self._mic_read = mic_read_chunk
        self._closed.clear()
        self._started.clear()
        self._error = None
        self.player.start()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="gpt-live-session")
        self._thread.start()
        if not self._started.wait(timeout=30):
            self.close()
            raise TimeoutError("GPT-Live session.started timed out")
        if self._error is not None:
            raise RuntimeError(f"GPT-Live failed to start: {self._error}") from self._error
        logger.info(
            "live.session.started session_id=%s model=%s backend=%s",
            self.session_id,
            self._live_model,
            self._backend_model,
        )

    def run_until_idle(self) -> None:
        """Block until sleep/voice restart or fatal error."""
        while not self._closed.wait(timeout=0.1):
            if self._error is not None and not self._sleep_requested:
                raise RuntimeError(f"GPT-Live session error: {self._error}") from self._error
            if self._sleep_requested and self._closed.is_set():
                break
        if self._thread is not None:
            self._thread.join(timeout=20)

    def append_instruction(self, text: str) -> None:
        self._run_coro(self._append_instruction(text))

    def speak_context(self, text: str) -> None:
        self._run_coro(self._speak_context(text))

    def stop_playback(self) -> None:
        self.player.interrupt()

    def close(self) -> None:
        """Graceful close — do not wait forever for OAuth/browser worker threads."""
        self._sleep_requested = True
        self._session_closing = True
        loop = self._loop
        if loop is not None and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            try:
                fut.result(timeout=_SHUTDOWN_WAIT_S)
            except TimeoutError:
                logger.warning(
                    "live.session.closed shutdown timeout session_id=%s — forcing local cleanup",
                    self.session_id,
                )
                try:
                    loop.call_soon_threadsafe(self._cancel_owned_tasks_soon)
                except Exception:
                    pass
            except Exception:
                logger.exception("live.session.closed shutdown error session_id=%s", self.session_id)
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.player.close()
        logger.info("live.session.closed session_id=%s", self.session_id)

    def _cancel_owned_tasks_soon(self) -> None:
        for task in list(self._tool_tasks) + list(self._tasks):
            if not task.done():
                task.cancel()

    # ── thread / asyncio ──────────────────────────────────────────────────────

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:
            self._error = exc
            logger.exception("live.error session_id=%s", self.session_id)
        finally:
            self._closed.set()
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._loop = None

    def _run_coro(self, coro) -> Any:
        loop = self._loop
        if loop is None or not loop.is_running():
            return None
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

    async def _async_main(self) -> None:
        assert self._mic_read is not None
        self._client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        session_config = self._session_config()
        async with self._client:
            async with self._client.live.connect() as connection:
                self._connection = connection
                await connection.session.start(session=session_config, event_id="event_start")
                sender = asyncio.create_task(self._send_audio_loop())
                self._tasks.add(sender)
                try:
                    async for event in connection:
                        await self._handle_event(event)
                        if self._sleep_requested and _event_attr(event, "type") == "session.closed":
                            break
                        if self._sleep_requested and not self._closed.is_set():
                            # Request graceful close once; keep reading until session.closed.
                            if not getattr(self, "_close_sent", False):
                                self._close_sent = True
                                try:
                                    await connection.session.close()
                                except Exception:
                                    logger.exception("live.error session.close failed")
                                    break
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                    self._tasks.discard(sender)
                    self._connection = None

    def _session_config(self) -> dict[str, Any]:
        return {
            "model": self._live_model,
            "instructions": self._live_instructions,
            "audio": {
                "format": {"type": "audio/pcm", "rate": self._audio_rate},
                "output": {"voice": self._voice},
            },
            "delegation": {
                "type": "responses",
                "responses": {
                    "model": self._backend_model,
                    "instructions": self._backend_instructions,
                    "tools": self._backend_tools(),
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                },
            },
        }

    def _backend_tools(self) -> list[dict]:
        from tools.live_schemas import LIVE_BACKEND_TOOLS

        return LIVE_BACKEND_TOOLS

    async def _send_audio_loop(self) -> None:
        pending = b""
        assert self._mic_read is not None
        while not self._sleep_requested:
            try:
                chunk = await asyncio.to_thread(self._mic_read)
            except Exception as exc:
                logger.error("live.error microphone failure: %s", type(exc).__name__)
                self._error = exc
                self._sleep_requested = True
                break
            if not chunk or self._connection is None:
                await asyncio.sleep(0.01)
                continue
            data = pending + chunk
            complete = len(data) - (len(data) % 2)
            pending = data[complete:]
            if complete:
                audio_b64 = base64.b64encode(data[:complete]).decode("ascii")
                try:
                    await self._connection.session.input_audio.append(audio=audio_b64)
                except Exception as exc:
                    if self._sleep_requested:
                        break
                    logger.warning("live.error input_audio.append: %s", type(exc).__name__)
                    await asyncio.sleep(0.05)

    async def _handle_event(self, event: Any) -> None:
        etype = _event_attr(event, "type")
        if etype == "session.started":
            sess = _event_attr(event, "session")
            remote_id = _event_attr(sess, "id")
            if remote_id:
                self.session_id = str(remote_id)
            self._started.set()
            return
        if etype == "session.output_audio.delta":
            delta = _event_attr(event, "delta") or ""
            try:
                pcm = base64.b64decode(delta)
            except Exception:
                return
            self.player.enqueue(pcm)
            return
        if etype == "session.input_transcript.delta":
            frag = _event_attr(event, "delta") or ""
            # Local barge-in: WebSocket Live does not auto-stop our PCM speaker queue.
            if isinstance(frag, str) and frag.strip() and self.player.is_playing:
                now = time.monotonic()
                if now - self._last_barge_in_at >= _BARGE_IN_COOLDOWN_S:
                    self.player.interrupt()
                    self._last_barge_in_at = now
                    logger.info(
                        "live.barge_in session_id=%s source=input_transcript",
                        self.session_id,
                    )
            self._input_buf += frag
            if self._on_user_transcript:
                try:
                    self._on_user_transcript(frag)
                except Exception:
                    logger.exception("user transcript callback failed")
            return
        if etype == "session.output_transcript.delta":
            frag = _event_attr(event, "delta") or ""
            self._output_buf += frag
            return
        if etype == "session.delegation.created":
            delegation = _event_attr(event, "delegation")
            did = _event_attr(delegation, "id")
            logger.info(
                "live.delegation.created session_id=%s delegation_id=%s target=%s",
                self.session_id,
                did,
                _event_attr(delegation, "target"),
            )
            if did:
                rev = self._executor.revisions.current
                self._executor.revisions.bind_delegation(str(did), rev)
            return
        if etype == "response.event":
            await self._handle_response_envelope(event)
            return
        if etype == "error":
            logger.error(
                "live.error session_id=%s detail=%s",
                self.session_id,
                _event_attr(event, "error") or event,
            )
            return
        if etype == "session.closed":
            self._flush_transcript_buffers()
            self._closed.set()
            return

        # Commit turn boundaries opportunistically when buffers look settled —
        # GPT-Live does not guarantee a single "turn complete" transcript event.
        if etype in ("session.updated",):
            self._flush_transcript_buffers()

    async def _handle_response_envelope(self, outer: Any) -> None:
        delegation_id = _event_attr(outer, "delegation_id")
        inner = _event_attr(outer, "event")
        if inner is None:
            return
        itype = _event_attr(inner, "type")

        if itype == "response.created":
            response = _event_attr(inner, "response")
            rid = _event_attr(response, "id") or _event_attr(inner, "response_id")
            if rid and delegation_id:
                self._delegation_ids[str(rid)] = str(delegation_id)
                self._pending_tool_calls.setdefault(str(rid), set())
            return

        if itype == "response.output_item.done":
            # Re-wrap so extract_completed_function_call sees the outer envelope.
            completed = extract_completed_function_call(outer)
            if completed is None:
                return
            if completed.response_id:
                self._pending_tool_calls.setdefault(completed.response_id, set()).add(completed.call_id)
            logger.info(
                "live.backend.function_call session_id=%s delegation_id=%s call_id=%s tool_name=%s",
                self.session_id,
                completed.delegation_id,
                completed.call_id,
                completed.name,
            )
            # Do NOT await on the recv loop — blocking tools must not freeze mic/events.
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

            def _done(done_task: asyncio.Task, *, call_id: str = completed.call_id) -> None:
                self._tool_tasks.discard(done_task)
                if done_task.cancelled():
                    return
                exc = done_task.exception()
                if exc is not None:
                    logger.error(
                        "live.tool.completed unexpected error call_id=%s error=%s",
                        call_id,
                        type(exc).__name__,
                        exc_info=exc,
                    )

            task.add_done_callback(_done)
            return

    async def _execute_and_continue(
        self,
        *,
        name: str,
        arguments: Any,
        call_id: str,
        delegation_id: str | None,
        response_id: str | None,
    ) -> None:
        if self._connection is None:
            return
        ctx = ToolExecutionContext(
            session_id=self.session_id,
            delegation_id=delegation_id,
            user_utterances=[
                t["content"] for t in self._turns if t.get("role") == "user" and t.get("content")
            ]
            or None,
        )
        result = await self._executor.execute(name, arguments, ctx)
        # Session may have closed while a blocking OAuth/Google call ran in a worker thread.
        if self._session_closing or self._closed.is_set() or self._connection is None:
            logger.info(
                "live.tool.completed discarded session_id=%s call_id=%s tool_name=%s — session closed",
                self.session_id,
                call_id,
                name,
            )
            return
        result_json = result.to_json()
        if name == "end_conversation" and result.ok:
            self._sleep_requested = True
        if name == "change_voice" and result.ok:
            self.request_voice_restart()
        logger.info(
            "live.backend.function_result session_id=%s delegation_id=%s call_id=%s "
            "tool_name=%s status=%s op_id=%s task_revision=%s",
            self.session_id,
            delegation_id,
            call_id,
            name,
            result.status,
            result.op_id,
            ctx.task_revision,
        )

        try:
            await self._connection.response.item.create(
                event_id=f"tool_result_{call_id}",
                item={
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result_json,
                },
            )
        except Exception:
            if self._session_closing or self._closed.is_set():
                return
            raise
        if response_id and response_id in self._pending_tool_calls:
            self._pending_tool_calls[response_id].discard(call_id)
            # Continue only when all pending calls for this response have results.
            if self._pending_tool_calls[response_id]:
                return
        if self._session_closing or self._closed.is_set() or self._connection is None:
            return
        await self._connection.response.create(event_id=f"continue_{call_id}")
        logger.info(
            "live.backend.response_continued session_id=%s delegation_id=%s call_id=%s",
            self.session_id,
            delegation_id,
            call_id,
        )

    async def _append_instruction(self, text: str) -> None:
        if self._connection is None:
            return
        await self._connection.session.instructions.append(
            content=text, delegation_id=None, event_id=f"instr_{uuid.uuid4().hex[:8]}"
        )

    async def _speak_context(self, text: str) -> None:
        if self._connection is None:
            return
        await self._connection.session.commentary.append(
            content=text, delegation_id=None, event_id=f"comment_{uuid.uuid4().hex[:8]}"
        )

    async def _shutdown(self) -> None:
        self._session_closing = True
        self._sleep_requested = True
        for task in list(self._tool_tasks):
            if not task.done():
                task.cancel()
        if self._tool_tasks:
            await asyncio.gather(*list(self._tool_tasks), return_exceptions=True)
            self._tool_tasks.clear()
        if self._connection is not None and not self._close_sent:
            self._close_sent = True
            try:
                await asyncio.wait_for(self._connection.session.close(), timeout=2.0)
            except Exception:
                try:
                    await self._connection.close()
                except Exception:
                    pass

    def _flush_transcript_buffers(self) -> None:
        if self._input_buf.strip():
            self._turns.append({"role": "user", "content": self._input_buf.strip()})
            self._input_buf = ""
        if self._output_buf.strip():
            self._turns.append({"role": "assistant", "content": self._output_buf.strip()})
            self._output_buf = ""
