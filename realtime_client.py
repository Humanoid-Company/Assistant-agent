"""
OpenAI Realtime API conversation client.

Owns one persistent WebSocket session (model `gpt-realtime`) for the live,
post-wake-word conversation: streams microphone audio in, plays back model
audio out, and turns low-level session events into the handful of signals
`assistant.py` actually needs to react to (a finished user utterance, a tool
call). Replaces the old STT -> chat.completions -> TTS pipeline for anything
that happens while awake.

Turn detection runs server-side (`server_vad`) with `create_response=False` —
the caller decides when to trigger a reply. `interrupt_response=True` means
the server itself cancels an in-flight response the moment the user starts
talking; `RealtimePlayer.stop()` gives an immediate client-side cutoff on
top of that.

Barge-in requires the mic to not hear the assistant's own voice. This used
to run on the laptop's built-in mic + speakers with no hardware AEC, so mic
audio was fully dropped during playback (see git history) — echo-vs-real-
speech RMS heuristics were tried first and proved unreliable, and muting
was the only thing that stopped false self-interruption. Now that input
comes from a lavalier mic physically separated from the speakers, echo
pickup should be low enough that direct forwarding + server-side barge-in
works — this is what's being tried.
"""
import base64
import json
import logging
import queue
import threading
import time
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
from openai import OpenAI

from config import (
    OPENAI_API_KEY,
    REALTIME_MODEL,
    REALTIME_SILENCE_MS,
    REALTIME_VOICE,
    SAMPLE_RATE,
    STT_REALTIME_LANGUAGE,
    STT_REALTIME_MODEL,
)

logger = logging.getLogger(__name__)


def parse_tool_arguments(raw: object) -> dict:
    """Decode a Realtime function-call arguments payload.

    ``response.function_call_arguments.done`` is also emitted when a response is
    interrupted, so the string may be empty or not JSON. Values are not logged.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        logger.info("tool arguments empty")
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        logger.info("tool arguments unexpected type=%s", type(raw).__name__)
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.info("tool arguments invalid json error=%s length=%s", type(exc).__name__, len(raw))
        return {}
    if not isinstance(parsed, dict):
        logger.info("tool arguments not object type=%s", type(parsed).__name__)
        return {}
    return parsed

_RT_SAMPLE_RATE = 24_000  # Realtime API audio is fixed at 24 kHz; mic captures at 16 kHz.


def resample_16k_to_24k(chunk: bytes) -> bytes:
    """Linearly resample an int16 PCM chunk from 16 kHz to 24 kHz."""
    arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
    if len(arr) == 0:
        return chunk
    n_out = int(round(len(arr) * _RT_SAMPLE_RATE / SAMPLE_RATE))
    x_old = np.linspace(0, 1, len(arr), endpoint=False)
    x_new = np.linspace(0, 1, n_out, endpoint=False)
    resampled = np.interp(x_new, x_old, arr)
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


class RealtimePlayer:
    """Plays raw PCM16 @ 24kHz audio deltas from the Realtime API."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._stream: Optional[sd.RawOutputStream] = None
        self._thread: Optional[threading.Thread] = None
        self._ms_played = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        self._stream = sd.RawOutputStream(
            samplerate=_RT_SAMPLE_RATE, channels=1, dtype="int16"
        )
        self._stream.start()
        self._thread = threading.Thread(target=self._write_loop, daemon=True, name="rt-player")
        self._thread.start()

    def feed(self, pcm_chunk: bytes) -> None:
        self._queue.put(pcm_chunk)

    def reset_position(self) -> None:
        with self._lock:
            self._ms_played = 0.0

    @property
    def ms_played(self) -> float:
        with self._lock:
            return self._ms_played

    def stop(self) -> None:
        """Discard queued (not-yet-played) audio — used for barge-in.

        Deliberately does NOT touch the PortAudio stream itself (no abort/
        restart): the writer thread may be blocked inside a `write()` call on
        another thread at this exact moment, and aborting/restarting the
        stream concurrently with an in-flight write is a race that corrupts
        playback (crackling/garbled audio). Draining the queue is enough —
        the writer thread just blocks on the next `get()` once it's empty.
        """
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop_and_close(self) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass

    def _write_loop(self) -> None:
        while True:
            chunk = self._queue.get()
            if chunk is None:
                break
            try:
                self._stream.write(chunk)
                with self._lock:
                    self._ms_played += 1000.0 * (len(chunk) // 2) / _RT_SAMPLE_RATE
            except Exception as exc:
                logger.warning("Playback write error: %s", exc)


class RealtimeConversation:
    """One persistent Realtime API session for the live conversation."""

    def __init__(
        self,
        tools: list[dict],
        on_tool_call: Callable[[str, dict, str], Optional[str]],
        silent_tools: Optional[set[str]] = None,
        no_followup_tools: Optional[set[str]] = None,
        voice: str = REALTIME_VOICE,
    ) -> None:
        self._client = OpenAI(api_key=OPENAI_API_KEY)
        self._tools = tools
        # Overridable per-instance (assistant.py passes the user's saved
        # preference here) — the module-level REALTIME_VOICE from config.py
        # is only the default for a fresh install with no preference saved yet.
        self._voice = voice
        self._on_tool_call = on_tool_call
        # Tools that run alongside the model's normal spoken reply in the same
        # response (e.g. reporting detected vocal emotion) rather than instead
        # of one — these must NOT trigger a follow-up response.create(), since
        # there's no pending question the model is waiting to continue.
        self._silent_tools = silent_tools or set()
        # Tools whose result the caller replies to itself (e.g. end_conversation
        # — assistant.py speaks its own scripted farewell) — the automatic
        # follow-up response must be skipped, otherwise the model's own
        # generated reply plays right on top of/before that scripted line.
        self._no_followup_tools = no_followup_tools or set()

        self._cm = None
        self._conn = None
        self._send_lock = threading.Lock()
        self._events: "queue.Queue" = queue.Queue()
        self._closed = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._feeder_thread: Optional[threading.Thread] = None

        self.player = RealtimePlayer()

        self._session_config: dict = {}
        self._turns: list[dict] = []           # this session's turns (user/assistant text)
        self._collecting = False
        self._utterance_buf: list[bytes] = []
        self._assistant_transcript = ""
        self._next_response_scripted = False
        self._current_response_scripted = False
        # Tracks whether the CURRENT response is itself a forced silent-tool follow-up (see
        # response.done below) — used to allow that follow-up to call a real tool (e.g.
        # dispatch_task) instead of being speech-only, while still guaranteeing the forced-
        # follow-up chain can fire at most once per turn (never chains a second one), so it
        # can't loop forever even if the model calls another silent-only tool.
        self._next_response_is_forced_followup = False
        self._current_response_is_forced_followup = False
        # Set (possibly to "") whenever a user-utterance transcription
        # completes — consumed by pump_for_transcript() for deterministic
        # trigger-phrase matching (assistant.py) without a second, separate
        # STT pass that could disagree with what this session itself heard.
        self._last_transcript: Optional[str] = None
        self._silent_tool_used_this_response = False
        # Counts rather than a single flag: a tool call's function_call_output
        # triggers a follow-up create_response() *before* the tool-call
        # response's own response.done arrives, so a plain boolean would read
        # "done" after the first (tool-call-only) response while the real
        # follow-up reply hasn't even started streaming yet.
        self._response_done = threading.Event()
        self._response_done.set()  # idle until a response is actually created
        self._responses_requested = 0
        self._responses_completed = 0
        # Queue (not a single slot) of tool_choice values from create_response() calls that
        # arrived while a response was still in flight (server rejects overlapping
        # response.create with "already has an active response in progress") — each is fired,
        # in order, once the in-flight one finishes. A single boolean+value slot here previously
        # meant a SECOND deferred call while the first was still pending silently overwrote it,
        # losing that turn's reply outright rather than just delaying it — the same collision
        # that produced a fabricated "не вдалося знайти" reply once (see config.py's guardrail
        # against inventing a task result, added alongside this fix) instead of the real,
        # already-computed dispatch_task answer.
        self._deferred_response_requests: list[Optional[str]] = []

        # Latency instrumentation.
        self._speech_stopped_at: Optional[float] = None
        self._response_requested_at: Optional[float] = None
        self._first_audio_at: Optional[float] = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, instructions: str, mic_read_chunk: Callable[[], bytes]) -> None:
        self._cm = self._client.realtime.connect(model=REALTIME_MODEL)
        self._conn = self._cm.__enter__()

        self._session_config = {
            "type": "realtime",
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": _RT_SAMPLE_RATE},
                    # Laptop/speaker setup (not headphones) — the mic picks up
                    # the assistant's own voice from the speakers. far_field
                    # noise reduction runs server-side before VAD/the model,
                    # cutting down false "user interrupted" triggers caused by
                    # that echo.
                    "noise_reduction": {"type": "far_field"},
                    "transcription": {
                        "model": STT_REALTIME_MODEL,
                        "language": STT_REALTIME_LANGUAGE,
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": False,
                        "interrupt_response": True,
                        "silence_duration_ms": REALTIME_SILENCE_MS,
                        "prefix_padding_ms": 300,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": _RT_SAMPLE_RATE},
                    "voice": self._voice,
                },
            },
            "tools": self._tools,
        }
        self._send({"type": "session.update", "session": self._session_config})

        self._closed.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name="rt-reader")
        self._reader_thread.start()
        self._feeder_thread = threading.Thread(
            target=self._feeder_loop, args=(mic_read_chunk,), daemon=True, name="rt-feeder"
        )
        self._feeder_thread.start()
        self.player.start()
        logger.info("Realtime session connected (model=%s, voice=%s)", REALTIME_MODEL, self._voice)

    def close(self) -> None:
        self._closed.set()
        self.player.stop_and_close()
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
        self._cm = None
        self._conn = None
        if self._feeder_thread is not None:
            self._feeder_thread.join(timeout=1.0)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
        # Drain anything left in the queue so a stale event can't leak into the
        # next session.
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break

    def _send(self, payload: dict) -> None:
        if self._conn is None:
            return
        try:
            with self._send_lock:
                self._conn.send(payload)
        except Exception as exc:
            logger.warning("Realtime send failed (%s): %s", payload.get("type"), exc)

    # ── Microphone feeder (background thread) ──────────────────────────────────

    def _feeder_loop(self, read_chunk: Callable[[], bytes]) -> None:
        logger.info("Mic feeder started.")
        chunks_sent = 0
        last_heartbeat = time.monotonic()
        try:
            while not self._closed.is_set():
                chunk = read_chunk()
                if not chunk:
                    continue
                if self._collecting:
                    self._utterance_buf.append(chunk)
                self._send({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(resample_16k_to_24k(chunk)).decode("ascii"),
                })
                chunks_sent += 1
                now = time.monotonic()
                if now - last_heartbeat > 5.0:
                    logger.info("Mic feeder alive: %d chunks sent so far.", chunks_sent)
                    last_heartbeat = now
        except Exception:
            logger.error("Mic feeder crashed — no more audio will reach the model!", exc_info=True)
        finally:
            logger.info("Mic feeder stopped (%d chunks sent).", chunks_sent)

    # ── Reader thread + event pump ───────────────────────────────────────────

    def _reader_loop(self) -> None:
        try:
            for event in self._conn:
                self._events.put(event)
                if self._closed.is_set():
                    break
        except Exception as exc:
            if not self._closed.is_set():
                logger.warning("Realtime connection lost: %s", exc)

    def pump(self, timeout: float = 0.05) -> Optional[bytes]:
        """
        Process queued events for up to *timeout* seconds.

        Returns the raw 16 kHz PCM of a user utterance if one just finished
        (speech_started -> speech_stopped) during this call, else None.
        """
        completed_pcm: Optional[bytes] = None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                event = self._events.get(timeout=remaining)
            except queue.Empty:
                break
            pcm = self._handle_event(event)
            if pcm is not None:
                completed_pcm = pcm
        return completed_pcm

    def pump_for_transcript(self, timeout: float = 1.5) -> Optional[str]:
        """Blocks until the input transcription for the utterance that just
        finished (speech_stopped) arrives, or *timeout* elapses.

        Used for deterministic robot-trigger matching: checking against this
        session's own transcription (instead of a second, separate STT pass)
        avoids the two engines disagreeing on what was said. Checks the
        already-arrived value first — the completion event may land inside
        the very same pump() call that returned the speech_stopped PCM.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._last_transcript is not None:
                text, self._last_transcript = self._last_transcript, None
                return text
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.pump(timeout=min(0.1, remaining))

    def wait_until_response_done(self, timeout: float = 8.0) -> None:
        """Block until the current in-flight response finishes (used before
        closing the session, so a farewell line isn't cut off)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._response_done.is_set():
                return
            self.pump(timeout=0.1)

    def _handle_event(self, event) -> Optional[bytes]:
        etype = getattr(event, "type", "")

        if etype == "input_audio_buffer.speech_started":
            logger.info("Speech started (VAD).")
            self.player.stop()
            self._collecting = True
            self._utterance_buf = []
            return None

        if etype == "input_audio_buffer.speech_stopped":
            self._collecting = False
            self._speech_stopped_at = time.monotonic()
            pcm = b"".join(self._utterance_buf)
            self._utterance_buf = []
            logger.info("Speech stopped (VAD) — captured %d bytes of PCM.", len(pcm))
            return pcm or None

        if etype == "conversation.item.input_audio_transcription.completed":
            text = (getattr(event, "transcript", "") or "").strip()
            if text:
                logger.info("[user] %s", text)
                self._turns.append({"role": "user", "content": text})
            self._last_transcript = text
            return None

        if etype == "response.created":
            self._current_response_scripted = self._next_response_scripted
            self._next_response_scripted = False
            self._current_response_is_forced_followup = self._next_response_is_forced_followup
            self._next_response_is_forced_followup = False
            self._response_done.clear()
            self.player.reset_position()
            self._assistant_transcript = ""
            self._first_audio_at = None
            self._silent_tool_used_this_response = False
            return None

        if etype == "response.output_audio.delta":
            delta_b64 = getattr(event, "delta", "")
            if delta_b64:
                if self._first_audio_at is None:
                    self._first_audio_at = time.monotonic()
                    if self._response_requested_at is not None:
                        logger.info(
                            "[latency] response.create -> first audio: %.2fs",
                            self._first_audio_at - self._response_requested_at,
                        )
                    if not self._current_response_scripted and self._speech_stopped_at is not None:
                        logger.info(
                            "[latency] end of speech -> first audio: %.2fs",
                            self._first_audio_at - self._speech_stopped_at,
                        )
                        self._speech_stopped_at = None
                self.player.feed(base64.b64decode(delta_b64))
            return None

        if etype == "response.output_audio_transcript.done":
            self._assistant_transcript = getattr(event, "transcript", "") or ""
            if self._assistant_transcript:
                logger.info("[assistant] %s", self._assistant_transcript)
            return None

        if etype == "response.function_call_arguments.done":
            call_id = getattr(event, "call_id", "")
            name = getattr(event, "name", "")
            args = parse_tool_arguments(getattr(event, "arguments", None))
            if args:
                logger.info("Tool call: %s fields=%s", name, ",".join(sorted(str(key) for key in args)))
            else:
                logger.info("Tool call: %s empty arguments", name)
            is_silent = name in self._silent_tools
            if is_silent:
                self._silent_tool_used_this_response = True
            no_followup = is_silent or name in self._no_followup_tools
            result = self._on_tool_call(name, args, call_id)
            if result is not None:
                # None means the handler is running the work in the
                # background (e.g. an HTTP call) and will report back later
                # via submit_deferred_tool_result — nothing to send yet.
                self._submit_tool_result(call_id, result, trigger_followup=not no_followup)
            return None

        if etype == "response.done":
            got_speech = not self._current_response_scripted and bool(self._assistant_transcript)
            if got_speech:
                self._turns.append({"role": "assistant", "content": self._assistant_transcript})
            self._assistant_transcript = ""
            self._responses_completed += 1
            if self._responses_completed >= self._responses_requested:
                self._response_done.set()
            response_status = getattr(getattr(event, "response", None), "status", None)
            if (
                not got_speech
                and not self._current_response_scripted
                and self._silent_tool_used_this_response
                and response_status == "completed"
                and not self._current_response_is_forced_followup
            ):
                # The model only called a silent tool (e.g. note_emotion) and said nothing out
                # loud — force one follow-up so the user still gets an actual spoken reply for
                # this turn. tool_choice "auto" (not "none") — a "none" here was blocking the
                # model from calling a REAL tool (e.g. dispatch_task) it intended to call right
                # after note_emotion in the same turn, leaving it able only to say "I'll do that
                # now" without ever actually being allowed to do it (reported bug: reminders the
                # model verbally promised never got created). The
                # `not self._current_response_is_forced_followup` guard is what keeps this from
                # chaining forever instead — at most one forced follow-up per silent-only turn,
                # even if that follow-up is itself silent-only again.
                # Only for status == "completed": a "cancelled" response means the user already
                # interrupted it (barge-in), and a new response for their new utterance is
                # already on the way — forcing a follow-up here would fire a second, unwanted
                # reply on top of it (the reported "overlapping answers" bug).
                logger.info("Silent tool call left the turn without speech — forcing a follow-up (tools allowed).")
                self._next_response_is_forced_followup = True
                self._request_response()
                self._send({"type": "response.create", "response": {"tool_choice": "auto"}})
            elif self._response_done.is_set() and self._deferred_response_requests:
                # One or more create_response() calls arrived while this one was still in
                # flight and got queued (see create_response()) — fire the OLDEST one now that
                # we're actually idle, so it doesn't just silently go unanswered. Any further
                # queued requests wait for THIS new response's own response.done to dequeue in
                # turn, one at a time — never dropped, never all fired at once.
                tool_choice = self._deferred_response_requests.pop(0)
                self.create_response(tool_choice=tool_choice)
            return None

        if etype == "error":
            logger.warning("Realtime API error event: %s", event)
            return None

        return None

    # ── Speaking ──────────────────────────────────────────────────────────────

    def say(self, text: str) -> None:
        """Speak a scripted line verbatim (wake ack, farewell, prompts).

        Kept out of the persisted conversation (`conversation: none`).
        `tool_choice: "none"` — a scripted line must never itself trigger a
        tool call (e.g. restored history ending on a goodbye was making the
        model call end_conversation during the "Слухаю!" greeting instead of
        just saying it, instantly ending the brand new session).

        Blocks briefly if a response is still in flight (e.g. a robot-trigger
        confirmation firing right as a forced silent-tool follow-up from the
        previous turn is still wrapping up) — the server rejects an
        overlapping response.create outright, and `say()`'s call sites are
        short scripted lines where a brief wait is harmless, unlike the tight
        polling loop that calls create_response().
        """
        if not self._response_done.is_set():
            self.wait_until_response_done()
        self._next_response_scripted = True
        self._request_response()
        self._send({
            "type": "response.create",
            "response": {
                "conversation": "none",
                "instructions": f"Скажи рівно це, дослівно, нічого від себе не додаючи: «{text}»",
                "output_modalities": ["audio"],
                "tool_choice": "none",
            },
        })

    def create_response(self, tool_choice: Optional[str] = None) -> None:
        """Ask the model for a normal conversational reply.

        No-ops (deferring instead — see the response.done handler) if a
        response is already in flight: this is what was producing the
        server's "Conversation already has an active response in progress"
        rejection — a forced silent-tool follow-up (response.done handler)
        and a freshly completed user turn (assistant.py's main loop) can both
        land in the same pump() cycle and both try to fire a response.create.

        `tool_choice="none"` is how a caller reading back a reply that ends
        in a yes/no confirmation question (e.g. dispatch_task's "...
        Підтвердити?") stops the model from ALSO calling a tool in that same
        turn — with the session's default tool_choice ("auto"), nothing
        stops the model from speaking the question and then immediately
        continuing on its own into a tool call with no real user reply in
        between (reported bug: the model called dispatch_task("так") and
        booked a real meeting a second after asking to confirm, with no
        VAD/speech event in between). Only the NEXT genuine user turn
        (assistant.py's own create_response() call, no override) restores
        normal "auto" tool access.
        """
        if not self._response_done.is_set():
            logger.info(
                "create_response() deferred — a response is already in flight (%d already queued).",
                len(self._deferred_response_requests),
            )
            self._deferred_response_requests.append(tool_choice)
            return
        self._next_response_scripted = False
        self._request_response()
        payload: dict = {"type": "response.create"}
        if tool_choice is not None:
            payload["response"] = {"tool_choice": tool_choice}
        self._send(payload)

    def _request_response(self) -> None:
        self._responses_requested += 1
        self._response_done.clear()
        self._response_requested_at = time.monotonic()

    def _submit_tool_result(
        self, call_id: str, output: str, trigger_followup: bool = True, allow_tool_calls: bool = True
    ) -> None:
        self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        })
        if trigger_followup:
            self.create_response(tool_choice=None if allow_tool_calls else "none")

    def submit_deferred_tool_result(
        self, call_id: str, output: str, trigger_followup: bool = True, allow_tool_calls: bool = True
    ) -> None:
        """Report a tool result that a background thread finished computing
        after `on_tool_call` already returned None for it (e.g. a slow HTTP
        call to an external service) — safe to call from any thread.

        `allow_tool_calls=False` is for a reply that itself asks the user a
        yes/no confirmation question — see create_response()'s docstring for
        why that's needed to stop the model from confirming its own question.
        """
        self._submit_tool_result(
            call_id, output, trigger_followup=trigger_followup, allow_tool_calls=allow_tool_calls
        )

    def update_instructions(self, instructions: str) -> None:
        self._session_config["instructions"] = instructions
        self._send({"type": "session.update", "session": self._session_config})

    def update_transcription_language(self, language: str) -> None:
        """Unlike output voice (fixed for the connection's lifetime), the
        input transcription language is just another session field — a plain
        session.update applies it live, no reconnect needed."""
        self._session_config["audio"]["input"]["transcription"]["language"] = language
        self._send({"type": "session.update", "session": self._session_config})

    # ── Context / history injection ──────────────────────────────────────────

    def inject_context_item(self, role: str, text: str) -> None:
        """Add a text item to the live conversation without generating a reply."""
        content_type = "output_text" if role == "assistant" else "input_text"
        self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, "text": text}],
            },
        })
        self._turns.append({"role": role, "content": text})

    def inject_history(self, turns: list[dict]) -> None:
        """Restore previously-saved turns from an earlier awake session."""
        for turn in turns:
            content = turn.get("content", "")
            if content:
                self.inject_context_item(turn.get("role", "user"), content)

    def get_turns(self) -> list[dict]:
        """Everything said so far this session — restored + live turns."""
        return list(self._turns)
