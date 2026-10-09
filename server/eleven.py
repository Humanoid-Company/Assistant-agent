"""ElevenLabs test engine for the web page (web/eleven.html), side by side with GPT-Live.

The browser talks to OpenAI Realtime over WebRTC with text-only output: Realtime hears the user
and writes the answer; the page has each sentence voiced by ElevenLabs through /api/eleven/tts
(the ElevenLabs key stays here). Like the Live bridge, a sideband on the same call runs the tools.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from config import ELEVENLABS_API_KEY, REALTIME_MODEL, REALTIME_SILENCE_MS, STT_REALTIME_MODEL
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.live_schemas import LIVE_BACKEND_TOOLS
from voice.conversation import ConversationLog, remember_tool_result

logger = logging.getLogger(__name__)

ELEVEN_API = "https://api.elevenlabs.io"
# Offered on the page, fastest first. language_code (forcing Ukrainian) only works on the v2.5 ones.
ELEVEN_MODELS: dict[str, str] = {
    "eleven_flash_v2_5": "Flash v2.5 — найшвидша",
    "eleven_turbo_v2_5": "Turbo v2.5",
    "eleven_multilingual_v2": "Multilingual v2 — найякісніша",
    "eleven_v3": "v3 — найвиразніша, повільна",
}
_LANGUAGE_CODE_MODELS = {"eleven_flash_v2_5", "eleven_turbo_v2_5"}
# One sentence or two at a time; a cap so a runaway reply can't burn the month's credits.
MAX_TTS_CHARS = 800
# Voice and its settings are picked on the page in this mode.
ELEVEN_TOOLS: list[dict] = [t for t in LIVE_BACKEND_TOOLS if t["name"] not in ("change_voice", "set_voice_style")]
_VOICES_TTL_S = 600.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class TtsRequest(BaseModel):
    text: str
    voice_id: str
    model: str = "eleven_flash_v2_5"
    stability: float = 0.5
    similarity: float = 0.75
    style: float = 0.0
    speed: float = 1.0
    speaker_boost: bool = True
    previous_text: str = ""  # the sentence before: keeps the intonation joined up
    language: str = "uk"


def tts_payload(req: TtsRequest) -> dict:
    """ElevenLabs request body from the page's settings, every value kept in its allowed range."""
    model = req.model if req.model in ELEVEN_MODELS else "eleven_flash_v2_5"
    payload: dict[str, Any] = {
        "text": req.text.strip()[:MAX_TTS_CHARS],
        "model_id": model,
        "voice_settings": {
            "stability": _clamp(req.stability, 0.0, 1.0),
            "similarity_boost": _clamp(req.similarity, 0.0, 1.0),
            "style": _clamp(req.style, 0.0, 1.0),
            "speed": _clamp(req.speed, 0.7, 1.2),
            "use_speaker_boost": req.speaker_boost,
        },
    }
    if req.previous_text.strip() and model != "eleven_v3":
        payload["previous_text"] = req.previous_text.strip()[-MAX_TTS_CHARS:]
    if model in _LANGUAGE_CODE_MODELS and req.language in ("uk", "en", "ru"):
        payload["language_code"] = req.language
    return payload


class ElevenLabs:
    def __init__(self, api_key: str = ELEVENLABS_API_KEY) -> None:
        self.api_key = api_key
        self._http = httpx.AsyncClient(base_url=ELEVEN_API, timeout=httpx.Timeout(30.0, connect=10.0))
        self._voices: list[dict] = []
        self._voices_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def voices(self) -> list[dict]:
        """The account's voices (premade + added from the library), cached for a few minutes."""
        if self._voices and time.monotonic() - self._voices_at < _VOICES_TTL_S:
            return self._voices
        res = await self._http.get("/v1/voices", headers={"xi-api-key": self.api_key})
        res.raise_for_status()
        voices = []
        for v in res.json().get("voices", []):
            labels = v.get("labels") or {}
            voices.append({
                "id": v.get("voice_id"),
                "name": v.get("name") or "?",
                "gender": labels.get("gender") or "",
                "description": ", ".join(
                    str(labels[k]) for k in ("accent", "age", "description", "use_case", "descriptive") if labels.get(k)
                ),
                "category": v.get("category") or "",
            })
        self._voices = [v for v in voices if v["id"]]
        self._voices_at = time.monotonic()
        return self._voices

    async def tts(self, req: TtsRequest) -> AsyncIterator[bytes]:
        """MP3 chunks as ElevenLabs produces them. Raises before the first chunk on an API error,
        so the endpoint can still answer with a proper status."""
        payload = tts_payload(req)
        request = self._http.build_request(
            "POST",
            f"/v1/text-to-speech/{req.voice_id}/stream",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self.api_key},
            json=payload,
        )
        started = time.monotonic()
        response = await self._http.send(request, stream=True)
        if response.status_code != 200:
            detail = (await response.aread()).decode(errors="replace")[:300]
            await response.aclose()
            raise ElevenLabsError(response.status_code, detail)
        logger.info(
            "eleven.tts model=%s chars=%s headers_ms=%s", payload["model_id"], len(payload["text"]),
            int((time.monotonic() - started) * 1000),
        )

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return body()


class ElevenLabsError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"ElevenLabs {status}: {detail}")
        self.status = status
        self.detail = detail


def realtime_session(*, instructions: str, language: str) -> dict:
    """Realtime call that hears the user and answers in text only (ElevenLabs does the voice)."""
    return {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": instructions,
        "output_modalities": ["text"],
        "audio": {
            "input": {
                "noise_reduction": {"type": "near_field"},
                "transcription": {"model": STT_REALTIME_MODEL, "language": language},
                "turn_detection": {
                    "type": "server_vad",
                    "silence_duration_ms": REALTIME_SILENCE_MS,
                    "create_response": True,
                    "interrupt_response": True,
                },
            }
        },
        "tools": ELEVEN_TOOLS,
        "tool_choice": "auto",
    }


def history_items(live_input: list[dict]) -> list[dict]:
    """The conversation so far (ConversationLog.live_input) as Realtime conversation items."""
    items = []
    for item in live_input:
        role = item.get("role")
        text = "".join(part.get("text", "") for part in item.get("content") or [])
        if not text:
            continue
        if role == "assistant":
            items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
        else:
            items.append({
                "type": "message",
                "role": "user" if role == "user" else "system",
                "content": [{"type": "input_text", "text": text}],
            })
    return items


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class RealtimeToolBridge:
    """Sideband on a Realtime WebRTC call: seeds the history, runs tool calls, keeps the transcript.
    Same surface as SidebandToolBridge where the rest of the server touches it."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        call_id: str,
        executor: ToolExecutor,
        conversation: ConversationLog,
        history: list[dict],
        on_closed: Callable[[RealtimeToolBridge], None] | None = None,
        max_duration_s: float | None = None,
    ) -> None:
        self._client = client
        self.session_id = call_id
        self._executor = executor
        self._conversation = conversation
        self._history = history
        self._on_closed = on_closed
        self._max_duration_s = max_duration_s
        self._connection: Any = None
        self._closed = asyncio.Event()
        self._tool_tasks: set[asyncio.Task] = set()
        self._user_turns: list[str] = []
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
                logger.info("eleven.sideband.connected call_id=%s history=%s", self.session_id, len(self._history))
                for item in self._history:
                    await connection.send({"type": "conversation.item.create", "item": item})
                if self._max_duration_s:
                    expiry = asyncio.create_task(self._expire_after(self._max_duration_s))
                async for event in connection:
                    await self._handle_event(event)
        except Exception:
            logger.exception("eleven.sideband.failed call_id=%s", self.session_id)
        finally:
            self._closed.set()
            self._connection = None
            if expiry is not None:
                expiry.cancel()
            for task in list(self._tool_tasks):
                task.cancel()
            logger.info("eleven.sideband.closed call_id=%s", self.session_id)
            if self._on_closed:
                self._on_closed(self)

    async def _expire_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        logger.info("eleven.session.expired call_id=%s", self.session_id)
        await self.close()

    async def close(self) -> None:
        if self.is_open:
            try:
                await self._client.realtime.calls.hangup(self.session_id)
            except Exception:
                logger.debug("eleven hangup failed", exc_info=True)
            try:
                await self._connection.close()
            except Exception:
                logger.debug("eleven sideband close failed", exc_info=True)

    async def say(self, text: str) -> None:
        """Have the assistant tell the user something (e.g. a Google login finished)."""
        if not self.is_open:
            return
        await self._connection.send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": f"Tell the user briefly: {text}"}]},
        })
        await self._connection.send({"type": "response.create"})

    async def append_instruction(self, text: str, **_: Any) -> None:
        """Speed/style are ElevenLabs settings on the page in this mode."""

    async def _handle_event(self, event: Any) -> None:
        etype = _attr(event, "type")
        if etype == "conversation.item.input_audio_transcription.completed":
            text = (_attr(event, "transcript") or "").strip()
            if text:
                self._conversation.add("user", text)
                self._conversation.end_turn()
                self._user_turns = (self._user_turns + [text])[-20:]
            return
        if etype == "response.output_text.done":
            text = (_attr(event, "text") or "").strip()
            if text:
                self._conversation.add("assistant", text)
                self._conversation.end_turn()
            return
        if etype == "error":
            logger.error("eleven.realtime.error call_id=%s detail=%s", self.session_id, _attr(event, "error") or event)
            return
        if etype != "response.done":
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
        ctx = ToolExecutionContext(session_id=self.session_id, delegation_id=None, user_utterances=list(self._user_turns) or None)
        results = await asyncio.gather(
            *(self._executor.execute(_attr(c, "name"), _attr(c, "arguments"), ctx) for c in calls),
            return_exceptions=True,
        )
        if not self.is_open:
            return
        for call, result in zip(calls, results, strict=True):
            name = _attr(call, "name")
            if isinstance(result, BaseException):
                logger.error("eleven.tool.failed tool_name=%s", name, exc_info=result)
                output = '{"ok": false, "status": "error", "message": "Інструмент не спрацював."}'
            else:
                logger.info("eleven.tool.result call_id=%s tool_name=%s status=%s", self.session_id, name, result.status)
                if name == "end_conversation" and result.ok:
                    self.end_requested = True
                remember_tool_result(self._conversation, name, result.message)
                output = result.to_json()
            await self._connection.send({
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": _attr(call, "call_id"), "output": output},
            })
        await self._connection.send({"type": "response.create", "event_id": f"continue_{uuid.uuid4().hex[:8]}"})
