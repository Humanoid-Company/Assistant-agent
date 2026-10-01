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
from robot_control import ROBOT_ACTIONS
from robot_triggers import _ROBOT_ACTION_TEXT, _match_robot_trigger
from tools.executor import ToolExecutionContext
from tools.results import ToolResult, agent_result_to_tool_result
from tools.router_bridge import _run_connectivity_checks
from voice.base import State
from voice.live_session import LiveVoiceSession
from voice.mic import LiveMicCapture
from voice.options import LANGUAGE_OPTIONS, VOICE_OPTIONS, _sanitize_name

logger = logging.getLogger(__name__)


class LiveDriverMixin:
    # Set up by Assistant.__init__ / the awake-session loop.
    _live: LiveVoiceSession | None
    _running: bool
    _history: list[dict]
    _history_cutoff: int | None
    _pending_cutoff: int
    _live_user_frag: str
    _live_pending_robot: str | None
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
            on_user_transcript=self._on_live_user_transcript_fragment,
        )
        self._live = live
        try:
            live.connect(
                build_live_prompt(
                    language_name=LANGUAGE_OPTIONS.get(self._memory.get("language", "uk"), LANGUAGE_OPTIONS["uk"]),
                    assistant_name=self._memory.get("assistant_name"),
                    today=date.today().isoformat(),
                ),
                mic_read_chunk=mic.read_chunk,
                backend_instructions=build_backend_prompt(
                    today=date.today().isoformat(),
                    language_name=LANGUAGE_OPTIONS.get(self._memory.get("language", "uk"), LANGUAGE_OPTIONS["uk"]),
                ),
            )
            live.speak_context("Слухаю!")
            if alert := self._connectivity_watcher.pop_alert():
                live.speak_context(alert)
            if pending := self._pop_deferred_announcement():
                live.speak_context(pending)
            t_start = time.monotonic()
            while self._running and not live.sleep_requested and not self._sleep_requested:
                # Live owns turn-taking; we only poll lifecycle flags + local robot safety.
                action = self._match_pending_robot_from_live()
                if action:
                    self._execute_robot_trigger_live(action)
                time.sleep(0.05)
            logger.info("[latency] Live awake session duration: %.1fs", time.monotonic() - t_start)
            live.stop_playback()
            if not live.voice_restart_requested and not self._voice_change_pending:
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
                self.state = State.AWAKE
            else:
                self.state = State.SLEEPING
                time.sleep(1.5)

    def _on_live_user_transcript_fragment(self, fragment: str) -> None:
        # Accumulate for local robot fast-path; full turns flush inside LiveVoiceSession.
        buf = getattr(self, "_live_user_frag", "") + fragment
        self._live_user_frag = buf
        action = _match_robot_trigger(buf.lower())
        if action:
            self._live_pending_robot = action
            self._live_user_frag = ""

    def _match_pending_robot_from_live(self) -> str | None:
        action = getattr(self, "_live_pending_robot", None)
        self._live_pending_robot = None
        return action

    def _execute_robot_trigger_live(self, action: str) -> None:
        method = getattr(self.robot, action, None)
        if method is None or self._live is None:
            return
        try:
            method()
            self._live.speak_context(_ROBOT_ACTION_TEXT.get(action, "Готово."))
        except Exception as exc:
            logger.error("Robot trigger action %r failed: %s", action, exc, exc_info=True)
            self._live.speak_context("Не вдалося виконати команду роботом.")

    def _live_set_name(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        new_name = _sanitize_name(args.get("name", ""))
        if not new_name:
            return ToolResult(ok=False, status="error", message="Не зрозумів нового імені.")
        msg = self._set_assistant_name(new_name)
        return ToolResult(ok=True, status="ok", message=msg)

    def _live_change_voice(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        voice = str(args.get("voice", "")).strip().lower()
        if voice not in VOICE_OPTIONS:
            return ToolResult(
                ok=False,
                status="error",
                message=f"Голос {voice!r} не підтримується — скажи користувачу спробувати ще раз.",
            )
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
                f"Голос змінено на {voice}. Зараз коротко попрощаюсь і одразу продовжу новим голосом "
                "без перезапуску програми."
            ),
        )

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

    def _live_control_robot(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        action = str(args.get("action", "")).strip().lower()
        method = getattr(self.robot, action, None) if action in ROBOT_ACTIONS else None
        if method is None:
            return ToolResult(ok=False, status="error", message=f"Команда {action!r} не підтримується.")
        try:
            method()
            return ToolResult(ok=True, status="ok", message=_ROBOT_ACTION_TEXT.get(action, "Готово."))
        except Exception as exc:
            logger.error("Robot action %r failed: %s", action, exc, exc_info=True)
            return ToolResult(ok=False, status="error", message="Не вдалося виконати команду роботом.")

    def _live_google_account(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        return agent_result_to_tool_result(self._google_account(args))

    def _live_web_search(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        return self._web_search_tool_result(
            args,
            session_id=context.session_id,
            delegation_id=context.delegation_id,
        )
