"""The web page's «Realtime» engine, beside GPT-Live.

The browser talks to OpenAI Realtime over WebRTC: with an OpenAI preset voice Realtime speaks
itself; with ElevenLabs it answers in text and the page has it voiced (/api/eleven/tts).

Realtime has no delegation of its own, so the Google work goes through one tool, backend_task,
which runs the same Responses backend as Live (model, prompt, tools) — the «brain». It keeps its
thread across calls (previous_response_id), so a «так» to its confirmation question still finds
the op_id. Session tools and web search Realtime calls directly: they need no reasoning.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from config import REALTIME_MODEL, REALTIME_SILENCE_MS, REALTIME_TURN_DETECTION, STT_REALTIME_MODEL
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.live_schemas import LIVE_BACKEND_TOOLS
from voice.conversation import ConversationLog, remember_tool_result

logger = logging.getLogger(__name__)

# Realtime calls these itself: quick, nothing to reason about.
_DIRECT_TOOLS = {"web_search", "check_connection", "set_assistant_name", "change_language", "end_conversation"}
# Voice and its settings are chosen on the page in this engine.
_PAGE_ONLY_TOOLS = {"change_voice", "set_voice_style"}
BRAIN_TOOLS: list[dict] = [
    t for t in LIVE_BACKEND_TOOLS if t["name"] not in _DIRECT_TOOLS | _PAGE_ONLY_TOOLS
]
BACKEND_TASK_TOOL: dict = {
    "type": "function",
    "name": "backend_task",
    "description": (
        "Google Calendar, Gmail, notes (Google Docs) and the Google account: hand the request to the "
        "backend, which does it and returns what to tell the user. Include everything it needs: the "
        "user's words, the details from the conversation, and their yes/no to a question it asked."
    ),
    "parameters": {
        "type": "object",
        "properties": {"request": {"type": "string", "description": "The full request, in the user's language."}},
        "required": ["request"],
        "additionalProperties": False,
    },
}
REALTIME_TOOLS: list[dict] = [BACKEND_TASK_TOOL] + [t for t in LIVE_BACKEND_TOOLS if t["name"] in _DIRECT_TOOLS]

# Speed is a real parameter here (Live has none): the page's «Темп» maps onto it.
_SPEED_VALUES = {"slow": 0.9, "normal": 1.0, "fast": 1.12}
_HISTORY_CHARS = 8000
_BRAIN_MAX_STEPS = 8


def history_text(log: ConversationLog, max_chars: int = _HISTORY_CHARS) -> str:
    """The conversation so far as plain lines, newest kept (for the Realtime instructions)."""
    lines: list[str] = []
    for item in log.live_input():
        text = "".join(part.get("text", "") for part in item.get("content") or []).strip()
        if not text:
            continue
        role = item.get("role")
        lines.append(("User: " if role == "user" else "You: " if role == "assistant" else "Note: ") + text)
    out = "\n".join(lines)
    return out[-max_chars:]


def _turn_detection() -> dict:
    # The page decides whether to answer, once it knows the words and whether the voice was near
    # the mic: a cough, a quiet «угу» or the TV is a turn to VAD, and Realtime (unlike Live) answered
    # them. For the same reason a sound doesn't cut her off — the page's barge-in does that.
    if REALTIME_TURN_DETECTION == "server":
        return {
            "type": "server_vad",
            "silence_duration_ms": REALTIME_SILENCE_MS,
            "create_response": False,
            "interrupt_response": False,
        }
    # The end of a turn judged by meaning, like Live: no cut-off at a pause mid-thought.
    return {"type": "semantic_vad", "eagerness": "auto", "create_response": False, "interrupt_response": False}


def realtime_session(
    *, instructions: str, language: str, voice: str | None, speed: str = "normal"
) -> dict:
    """voice=None: text answers (ElevenLabs voices them on the page); else the Realtime preset voice."""
    audio: dict[str, Any] = {
        "input": {
            "noise_reduction": {"type": "near_field"},
            "transcription": {"model": STT_REALTIME_MODEL, "language": language},
            "turn_detection": _turn_detection(),
        }
    }
    if voice:
        audio["output"] = {"voice": voice, "speed": _SPEED_VALUES.get(speed, 1.0)}
    return {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": instructions,
        "output_modalities": ["audio"] if voice else ["text"],
        "audio": audio,
        "tools": REALTIME_TOOLS,
        "tool_choice": "auto",
    }


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class Brain:
    """backend_task: the Live backend (Responses model + the Google tools), run by us."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        executor: ToolExecutor,
        model: str,
        instructions: str,
        state: dict,
        conversation: ConversationLog | None = None,
        effort: str = "",
        parallel_tools: bool = True,
    ) -> None:
        self._client = client
        self._executor = executor
        self._model = model
        self._instructions = instructions
        self._state = state  # per browser: {"previous_id": …} survives new calls
        self._conversation = conversation
        self._effort = effort
        self._parallel = parallel_tools
        self._lock = asyncio.Lock()

    async def run(self, request: str, ctx: ToolExecutionContext) -> str:
        async with self._lock:  # one thread of thought: tasks run in order
            return await self._run(request, ctx)

    async def _run(self, request: str, ctx: ToolExecutionContext) -> str:
        recent = " | ".join((ctx.user_utterances or [])[-3:])
        text = request.strip() + (f"\n\nThe user's last words: {recent}" if recent else "")
        input_items: list[dict] = [{"role": "user", "content": text}]
        previous = self._state.get("previous_id")
        for _ in range(_BRAIN_MAX_STEPS):
            kwargs: dict[str, Any] = {
                "model": self._model,
                "instructions": self._instructions,
                "input": input_items,
                "tools": BRAIN_TOOLS,
                "parallel_tool_calls": self._parallel,
            }
            if previous:
                kwargs["previous_response_id"] = previous
            if self._effort:
                kwargs["reasoning"] = {"effort": self._effort}
            try:
                response = await self._client.responses.create(**kwargs)
            except Exception:
                if not previous:
                    raise
                # An expired or unknown thread: start a new one rather than fail the user.
                logger.warning("realtime.brain.thread_reset", exc_info=True)
                previous = None
                self._state.pop("previous_id", None)
                continue
            previous = response.id
            self._state["previous_id"] = previous
            calls = [item for item in (response.output or []) if _attr(item, "type") == "function_call"]
            if not calls:
                return (getattr(response, "output_text", "") or "").strip() or "Готово."
            outputs = await asyncio.gather(*(self._call(c, ctx) for c in calls))
            input_items = list(outputs)
        return "Не вдалося довести це до кінця — спробуй сказати ще раз."

    async def _call(self, call: Any, ctx: ToolExecutionContext) -> dict:
        name = _attr(call, "name")
        try:
            result = await self._executor.execute(name, _attr(call, "arguments"), ctx)
            remember_tool_result(self._conversation, name, result.message)
            output = result.to_json()
            logger.info("realtime.brain.tool tool_name=%s status=%s", name, result.status)
        except Exception:
            logger.exception("realtime.brain.tool_failed tool_name=%s", name)
            output = json.dumps({"ok": False, "status": "error", "message": "Інструмент не спрацював."}, ensure_ascii=False)
        return {"type": "function_call_output", "call_id": _attr(call, "call_id"), "output": output}


class RealtimeBridge:
    """Sideband on a Realtime WebRTC call: runs its tool calls (backend_task through the brain) and
    keeps the transcript. Same surface as SidebandToolBridge where the rest of the server touches it."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        call_id: str,
        executor: ToolExecutor,
        brain: Brain,
        conversation: ConversationLog,
        on_closed: Callable[[RealtimeBridge], None] | None = None,
        max_duration_s: float | None = None,
    ) -> None:
        self._client = client
        self.session_id = call_id
        self._executor = executor
        self._brain = brain
        self._conversation = conversation
        self._on_closed = on_closed
        self._max_duration_s = max_duration_s
        self._connection: Any = None
        self._closed = asyncio.Event()
        self._tool_tasks: set[asyncio.Task] = set()
        self._user_turns: list[str] = []
        # The page's «is this addressed to you?» checks (out of band): not part of the conversation.
        self._side_responses: set[str] = set()
        self.end_requested = False
        self.restart_for_voice = False  # set by change_voice on Live calls; nothing to restart here

    @property
    def busy(self) -> bool:
        return any(not task.done() for task in self._tool_tasks)

    @property
    def is_open(self) -> bool:
        return self._connection is not None and not self._closed.is_set()

    async def run(self) -> None:
        expiry: asyncio.Task | None = None
        try:
            async with self._client.realtime.connect(call_id=self.session_id) as connection:
                self._connection = connection
                logger.info("realtime.sideband.connected call_id=%s", self.session_id)
                if self._max_duration_s:
                    expiry = asyncio.create_task(self._expire_after(self._max_duration_s))
                async for event in connection:
                    await self._handle_event(event)
        except Exception:
            logger.exception("realtime.sideband.failed call_id=%s", self.session_id)
        finally:
            self._closed.set()
            self._connection = None
            if expiry is not None:
                expiry.cancel()
            for task in list(self._tool_tasks):
                task.cancel()
            logger.info("realtime.sideband.closed call_id=%s", self.session_id)
            if self._on_closed:
                self._on_closed(self)

    async def _expire_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        logger.info("realtime.session.expired call_id=%s", self.session_id)
        await self.close()

    async def close(self) -> None:
        if self.is_open:
            try:
                await self._client.realtime.calls.hangup(self.session_id)
            except Exception:
                logger.debug("realtime hangup failed", exc_info=True)
            try:
                await self._connection.close()
            except Exception:
                logger.debug("realtime sideband close failed", exc_info=True)

    async def say(self, text: str) -> None:
        """Have the assistant tell the user something (e.g. a Google login finished)."""
        if not self.is_open:
            return
        await self._send_system(f"Tell the user briefly: {text}")
        await self._connection.send({"type": "response.create"})

    async def append_instruction(self, text: str, **_: Any) -> None:
        """Context for the model without a reply (the Google sign-in help)."""
        if self.is_open and text:
            await self._send_system(text)

    async def _send_system(self, text: str) -> None:
        await self._connection.send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]},
        })

    async def _handle_event(self, event: Any) -> None:
        etype = _attr(event, "type")
        if etype == "response.created":
            response = _attr(event, "response")
            if (_attr(response, "metadata") or {}).get("purpose"):
                self._side_responses.add(_attr(response, "id"))
            return
        if _attr(event, "response_id") in self._side_responses:
            return
        if etype == "conversation.item.input_audio_transcription.completed":
            text = (_attr(event, "transcript") or "").strip()
            if text:
                self._conversation.add("user", text)
                self._conversation.end_turn()
                self._user_turns = (self._user_turns + [text])[-20:]
            return
        if etype in ("response.output_audio_transcript.done", "response.output_text.done"):
            text = (_attr(event, "transcript") or _attr(event, "text") or "").strip()
            if text:
                self._conversation.add("assistant", text)
                self._conversation.end_turn()
            return
        if etype == "error":
            logger.error("realtime.error call_id=%s detail=%s", self.session_id, _attr(event, "error") or event)
            return
        if etype != "response.done":
            return
        if _attr(_attr(event, "response"), "id") in self._side_responses:
            self._side_responses.discard(_attr(_attr(event, "response"), "id"))
            return
        calls = [
            item for item in (_attr(_attr(event, "response"), "output") or [])
            if _attr(item, "type") == "function_call"
        ]
        if calls:
            task = asyncio.create_task(self._run_tools(calls))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)

    async def _run_tools(self, calls: list[Any]) -> None:
        ctx = ToolExecutionContext(
            session_id=self.session_id, delegation_id=None, user_utterances=list(self._user_turns) or None
        )
        outputs = await asyncio.gather(*(self._run_tool(c, ctx) for c in calls))
        if not self.is_open:
            return
        for call, output in zip(calls, outputs, strict=True):
            await self._connection.send({
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": _attr(call, "call_id"), "output": output},
            })
        await self._connection.send({"type": "response.create", "event_id": f"continue_{uuid.uuid4().hex[:8]}"})

    async def _run_tool(self, call: Any, ctx: ToolExecutionContext) -> str:
        name = _attr(call, "name")
        try:
            if name == "backend_task":
                args = json.loads(_attr(call, "arguments") or "{}")
                answer = await self._brain.run(str(args.get("request") or ""), ctx)
                logger.info("realtime.backend_task call_id=%s", self.session_id)
                return json.dumps({"ok": True, "message": answer}, ensure_ascii=False)
            result = await self._executor.execute(name, _attr(call, "arguments"), ctx)
        except Exception:
            logger.exception("realtime.tool.failed tool_name=%s", name)
            return json.dumps({"ok": False, "status": "error", "message": "Не вдалося виконати."}, ensure_ascii=False)
        logger.info("realtime.tool.result call_id=%s tool_name=%s status=%s", self.session_id, name, result.status)
        if name == "end_conversation" and result.ok:
            self.end_requested = True
        remember_tool_result(self._conversation, name, result.message)
        return result.to_json()
