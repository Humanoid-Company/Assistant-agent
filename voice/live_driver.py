"""Assistant mixin: drives an awake session on the GPT-Live engine + its tool handlers."""
from __future__ import annotations

import logging
import time
import uuid
from datetime import date

from config import (
    OPENAI_LIVE_VOICE,
)
from prompts.backend_prompt import build_backend_prompt
from prompts.live_prompt import build_live_prompt
from tools.executor import ToolExecutionContext
from tools.results import ToolResult, agent_result_to_tool_result
from tools.router_bridge import _run_connectivity_checks
from voice.background import wake_commentary
from voice.base import State
from voice.conversation import ConversationLog
from voice.live_session import LiveVoiceSession
from voice.mic import LiveMicCapture
from voice.options import (
    LANGUAGE_OPTIONS,
    LIVE_VOICE_OPTIONS,
    SPEED_OPTIONS,
    STYLE_OPTIONS,
    _sanitize_name,
    asked_for_voice_change,
    delivery_instruction,
    voice_request_target,
)

logger = logging.getLogger(__name__)


# Said right after a wake phrase so the user hears Єва is on (same wording in web/app.js).
WAKE_GREETING = (
    "The user just called you: «{phrase}». You are back and listening — let them hear it. "
    "Reply right away in two to four words that match it: to «привіт», «вітаю», «гей» greet back "
    "warmly («Привіт! Що робимо?», «О, привіт! Слухаю»), otherwise a short «Так, слухаю» or "
    "«Слухаю тебе». Vary it, then wait."
)


class LiveDriverMixin:
    # Set up by Assistant.__init__ / the awake-session loop.
    _live: LiveVoiceSession | None
    _running: bool
    _history: list[dict]
    _history_cutoff: int | None
    _pending_cutoff: int
    _conversation: ConversationLog
    _wake_request: str
    _wake_phrase: str

    def _run_awake_session_live(self) -> None:
        """GPT-Live path: full duplex + Responses delegation. No manual turn create."""
        self._sleep_requested = False
        self._voice_change_pending = False
        self._history_cutoff = None
        self._pending_cutoff = len(self._history)
        self._router_session_id = str(uuid.uuid4())
        voice = self._memory.get("realtime_voice") or self._memory.get("live_voice") or OPENAI_LIVE_VOICE
        mic = LiveMicCapture(self.stt.read_chunk)
        mic.open()
        live = LiveVoiceSession(
            tool_executor=self._tool_executor,
            voice=voice,
            session_id=self._router_session_id,
            conversation=self._conversation,
            on_voice_request=lambda text: self._voice_by_request(text, voice),
        )
        voice_restart = getattr(self, "_voice_restarted", False)
        self._voice_restarted = False
        self._live = live
        try:
            live.connect(
                build_live_prompt(
                    language_name=LANGUAGE_OPTIONS.get(self._memory.get("language", "uk"), LANGUAGE_OPTIONS["uk"]),
                    assistant_name=self._memory.get("assistant_name"),
                    today=date.today().isoformat(),
                    voice=voice,
                    delivery=delivery_instruction(
                        self._memory.get("speed", "normal"), self._memory.get("style", "normal")
                    ),
                ),
                mic_read_chunk=mic.read_chunk,
                backend_instructions=build_backend_prompt(
                    today=date.today().isoformat(),
                    language_name=LANGUAGE_OPTIONS.get(self._memory.get("language", "uk"), LANGUAGE_OPTIONS["uk"]),
                ),
            )
            wake_request, self._wake_request = self._wake_request, ""
            # What she overheard during the pause goes in the same commentary as the wake reply.
            note = self._background.digest() if getattr(self, "_paused", False) and not voice_restart else ""
            self._paused = False
            if note:
                self._conversation.add_note(note)
                logger.info("background.digest chars=%s", len(note))
            heard = wake_commentary(note) + "\n\n" if note else ""
            if voice_restart:
                pass  # a new voice: no announcement, she just listens on with the same memory
            elif wake_request:
                live.speak_context(f"{heard}The user just said to you: «{wake_request}». Answer it.")
            elif self._wake_phrase:
                live.speak_context(heard + WAKE_GREETING.format(phrase=self._wake_phrase))
            else:
                live.speak_context(heard + "Слухаю!")
            if alert := self._connectivity_watcher.pop_alert():
                live.speak_context(alert)
            if pending := self._pop_deferred_announcement():
                live.speak_context(pending)
            t_start = time.monotonic()
            while self._running and not live.sleep_requested and not self._sleep_requested:
                # Live owns turn-taking; we only poll lifecycle flags.
                time.sleep(0.05)
            logger.info("[latency] Live awake session duration: %.1fs", time.monotonic() - t_start)
            live.stop_playback()
            # «Дякую, Єва» pauses: no goodbye, the conversation goes on after the next wake.
            if live.pause_requested:
                self._paused = True
                logger.info("Live paused by «Дякую, Єва» — listening in the background until the wake phrase.")
            elif not live.voice_restart_requested and not self._voice_change_pending:
                live.speak_context("До побачення!")
                time.sleep(1.0)
        except Exception:
            logger.error("Live awake session crashed; closing before sleep.", exc_info=True)
            raise
        finally:
            turns = live.get_turns()
            self._history = turns[: self._history_cutoff] if self._history_cutoff is not None else turns
            live.close()
            self._live = None
            mic.close()
            if live.voice_restart_requested or self._voice_change_pending:
                # Live: restart a new awake session with the new voice — do NOT kill the process.
                logger.info("Live voice change — restarting voice session without process exit.")
                self._voice_change_pending = False
                self._voice_restarted = True
                self.state = State.AWAKE
            else:
                self.state = State.SLEEPING
                time.sleep(1.5)

    def _live_set_name(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        new_name = _sanitize_name(args.get("name", ""))
        if not new_name:
            return ToolResult(ok=False, status="error", message="Не зрозумів нового імені.")
        msg = self._set_assistant_name(new_name)
        return ToolResult(ok=True, status="ok", message=msg)

    def _live_change_voice(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        voice = str(args.get("voice", "")).strip().lower()
        if not asked_for_voice_change(context.user_utterances):
            logger.info("live.voice.tool_refused voice=%s (user didn't mention the voice)", voice)
            return ToolResult(
                ok=False,
                status="error",
                message="Користувач не просив змінити голос — не змінюй його, просто продовжуй розмову.",
            )
        if voice not in LIVE_VOICE_OPTIONS:
            return ToolResult(
                ok=False,
                status="error",
                message=f"Голос {voice!r} не підтримується — скажи користувачу спробувати ще раз.",
            )
        if time.monotonic() - getattr(self, "_voice_switched_at", -1e9) < 15:
            return ToolResult(ok=True, status="ok", message="Голос уже змінено. Нічого про це не кажи.")
        self._voice_switched_at = time.monotonic()
        self._memory["realtime_voice"] = voice
        self._memory["live_voice"] = voice
        self._save_memory()
        self._voice_change_pending = True
        if self._live is not None:
            self._live.request_voice_restart()
        return ToolResult(
            ok=True,
            status="ok",
            message=(
                "Голос змінено. Нічого про це не кажи — розмова одразу продовжиться новим голосом."
            ),
        )

    def _voice_by_request(self, utterance: str, current: str) -> bool:
        """«Єва, зміни голос на …» heard in the transcript: switch without waiting for the model."""
        if time.monotonic() - getattr(self, "_voice_switched_at", -1e9) < 15:
            return False  # the model already switched for this same request
        voice = voice_request_target(utterance, current)
        if voice is None or voice == current:
            return False
        return self._live_change_voice({"voice": voice}, None).ok

    def _live_set_voice_style(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        """Speed/style of the current voice — instructions, applied from the next sentence."""
        del context
        speed = str(args.get("speed") or self._memory.get("speed", "normal")).strip().lower()
        style = str(args.get("style") or self._memory.get("style", "normal")).strip().lower()
        if speed not in SPEED_OPTIONS or style not in STYLE_OPTIONS:
            return ToolResult(ok=False, status="error", message="Темп: slow/normal/fast, стиль: calm/normal/expressive.")
        self._memory["speed"], self._memory["style"] = speed, style
        self._save_memory()
        if self._live is not None:
            self._live.append_instruction(delivery_instruction(speed, style, changed=True))
        return ToolResult(ok=True, status="ok", message="Готово, говорю так з наступного речення.")

    def _live_change_language(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        language = str(args.get("language", "")).strip().lower()
        if language not in LANGUAGE_OPTIONS:
            return ToolResult(
                ok=False,
                status="error",
                message=f"Мова {language!r} не підтримується — скажи користувачу спробувати ще раз.",
            )
        self._memory["language"] = language
        self._save_memory()
        lang_name = LANGUAGE_OPTIONS[language]
        if self._live is not None:
            self._live.append_instruction(f"From now on speak exclusively in {lang_name}.")
        return ToolResult(
            ok=True,
            status="ok",
            message=f"Мову змінено на {lang_name}. Наступну репліку скажи вже цією мовою.",
        )

    def _live_end_conversation(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del args, context
        self._sleep_requested = True
        self._history_cutoff = self._pending_cutoff
        if self._live is not None:
            self._live.request_sleep()
        return ToolResult(ok=True, status="ok", message="Розмову завершено.")

    def _live_check_connection(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del args, context
        return agent_result_to_tool_result(_run_connectivity_checks(self.router))

    def _live_google_account(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        return agent_result_to_tool_result(self._google_account(args))

    def _live_web_search(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        return self._web_search_tool_result(
            args,
            session_id=context.session_id,
            delegation_id=context.delegation_id,
        )
