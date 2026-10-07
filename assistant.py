"""
Main assistant state machine.

States
------
SLEEPING – Idle; only listens for the wake phrase (cheap Google STT).
AWAKE    – One persistent voice session (Realtime legacy OR GPT-Live)
           handles conversation. Engine selected via VOICE_ENGINE.

Conversation memory
--------------------
Turns are carried over in-memory between sleep/wake cycles within the same
process run — say goodbye and "привіт" again and the assistant still
remembers. Restarting the script clears it (nothing is persisted to disk).

Voice commands (handled as tool calls, not regex)
----------------------------------------------------------------
"тебе звати …"             – change the assistant's own name
"до побачення" / "бувай" / … — model calls end_conversation to go back to sleep
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date
from pathlib import Path

from agents.types import AgentResult
from config import (
    CONNECTIVITY_CHECK_INTERVAL_S,
    GOOGLE_ACCOUNT_STATE_FILE,
    GOOGLE_CALENDAR_TIMEZONE,
    GOOGLE_OAUTH_CLIENT_SECRETS_FILE,
    SYSTEM_PROMPT,
    VOICE_ENGINE,
    WEB_SEARCH_MAX_CALLS_PER_TURN,
)
from integrations.web_search import (
    WebSearchRateLimiter,
)
from realtime_client import RealtimeConversation
from router.factory import build_agent_router
from speech_to_text import SpeechToText
from text_to_speech import TextToSpeech
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutor
from tools.gmail_tools import GmailToolWrappers
from tools.notes_tools import NotesToolWrappers
from tools.realtime_schemas import TOOLS  # noqa: F401  (re-export for tests)
from tools.results import ToolResult
from tools.router_bridge import (  # noqa: F401  (several are re-exported for tests)
    _describe_connection_status,
    _model_tool_output,
    _parse_router_reply,
    _router_tool_failure_message,
    _run_connectivity_checks,
    _should_speak_router_result,
    calendar_tool_args,
)
from tools.task_context import TaskRevisionTracker
from tools.web_search_tool import web_search_tool_result
from voice.background import BackgroundLog, make_openai_summarizer
from voice.base import State
from voice.conversation import ConversationLog
from voice.factory import normalize_voice_engine
from voice.live_driver import LiveDriverMixin
from voice.live_session import LiveVoiceSession
from voice.options import LANGUAGE_OPTIONS, VOICE_OPTIONS
from voice.realtime_driver import RealtimeDriverMixin
from voice.wake_phrases import match_wake

_MEMORY_FILE = Path(__file__).parent / "assistant_memory.json"

# google_account actions that open Google's consent page in the browser.
_BROWSER_CONSENT_ACTIONS = frozenset({"connect", "reauth_switch", "grant_all", "grant_gmail", "grant_notes"})

logger = logging.getLogger(__name__)


class _ConnectivityWatcher:
    """Tracks connectivity state across background checks and arms a one-shot spoken alert on a
    healthy->broken transition — deliberately does NOT re-arm on every broken check while still
    down (that would repeat the same alert on every single wake until someone fixes it) and
    resets silently once it recovers, ready to alert again on the next real failure. Pure state
    machine, no network/audio dependency, so it's unit-testable on its own."""

    def __init__(self) -> None:
        self._ok = True
        self._pending: str | None = None
        self._lock = threading.Lock()

    def record_check_result(self, ok: bool, alert_text: str) -> None:
        with self._lock:
            if ok:
                # Recovered — even a still-unpopped alert about the outage that just resolved
                # itself is no longer worth interrupting the user's next wake for.
                self._pending = None
            elif self._ok:
                self._pending = alert_text
            self._ok = ok

    def pop_alert(self) -> str | None:
        with self._lock:
            alert, self._pending = self._pending, None
            return alert


class Assistant(RealtimeDriverMixin, LiveDriverMixin):
    """Voice assistant state machine — orchestration/lifecycle only."""

    def __init__(self) -> None:
        self.stt = SpeechToText()
        self.tts = TextToSpeech()
        self.rt: RealtimeConversation | None = None
        self._live: LiveVoiceSession | None = None
        self.state = State.SLEEPING
        self._running = False
        self._sleep_requested = False
        # Realtime: process restart required. Live: session restart only.
        self._voice_change_pending = False
        self._voice_engine = normalize_voice_engine(VOICE_ENGINE)
        # Outcome of a Google consent that finished while no voice session was open.
        self._deferred_announcement: str | None = None

        # Conversation history for the current run only — carries over between
        # sleep/wake cycles (in-memory), but resets when the process restarts.
        self._history: list[dict] = []
        # Live: the same history, independent of the voice session — a voice change or a wake
        # after «Дякую, Єва» starts a new Live session that is seeded with it.
        self._conversation = ConversationLog()
        self._wake_request: str = ""
        self._wake_phrase: str = ""
        # Paused by «Дякую, Єва» (not ended): what is said nearby is kept and handed to her on wake.
        self._paused = False
        self._background = BackgroundLog(self._summarize_background)

        # Long-term memory (persists between sessions)
        self._memory: dict = self._load_memory()


        # Local Google Agent Router (Calendar + Gmail) — Gmail stays on Realtime only.
        self.router = build_agent_router(
            client_secrets_file=GOOGLE_OAUTH_CLIENT_SECRETS_FILE,
            state_file=GOOGLE_ACCOUNT_STATE_FILE,
            timezone=GOOGLE_CALENDAR_TIMEZONE,
        )
        self._task_revisions = TaskRevisionTracker()
        self._web_search_limiter = WebSearchRateLimiter(max_per_turn=WEB_SEARCH_MAX_CALLS_PER_TURN)
        self._tool_executor = self._build_tool_executor()

        # Proactive connectivity monitoring — alerts only on real API/network errors,
        # not on "Google not connected yet" (that is expected before first login).
        self._connectivity_watcher = _ConnectivityWatcher()

    def _build_tool_executor(self) -> ToolExecutor:
        calendar = CalendarToolWrappers(self.router.calendar_action)
        gmail = GmailToolWrappers(self.router.gmail_action)
        notes = NotesToolWrappers(self.router.notes_action)
        executor = ToolExecutor(
            calendar=calendar,
            gmail=gmail,
            notes=notes,
            revisions=self._task_revisions,
        )
        # Fast local memory/state updates can stay on the Live loop.
        executor.register("set_assistant_name", self._live_set_name, run_in_thread=False)
        executor.register("change_voice", self._live_change_voice, run_in_thread=False)
        executor.register("set_voice_style", self._live_set_voice_style, run_in_thread=False)
        executor.register("change_language", self._live_change_language, run_in_thread=False)
        executor.register("end_conversation", self._live_end_conversation, run_in_thread=False)
        # Blocking network / browser / hardware work must leave the Live event loop.
        executor.register("check_connection", self._live_check_connection, run_in_thread=True)
        executor.register("google_account", self._live_google_account, run_in_thread=True)
        executor.register("web_search", self._live_web_search, run_in_thread=True)
        return executor

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        logger.info("Assistant started. voice_engine=%s", self._voice_engine)
        threading.Thread(target=self._connectivity_watch_loop, daemon=True, name="connectivity-watch").start()
        self.tts.speak("Єва готова. Покличте її: «Єва, скажи» або «Привіт, Єва».")

        while self._running:
            try:
                if self.state == State.SLEEPING:
                    self._handle_sleeping()
                elif self.state == State.AWAKE:
                    self._run_awake_session()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.error("Unhandled error in main loop: %s", exc, exc_info=True)
                self.state = State.SLEEPING

        self._cleanup()

    def stop(self) -> None:
        self._running = False
        if self.rt is not None:
            self.rt.close()
        if self._live is not None:
            self._live.close()

    # ── State handlers ────────────────────────────────────────────────────────

    def _handle_sleeping(self) -> None:
        text, _, _ = self.stt.listen()
        rest = match_wake(text) if text else None
        if rest is None and text and getattr(self, "_paused", False):
            self._background.add(text)  # the same STT that waits for «Єва, скажи» — no extra cost
        if rest is not None:
            logger.info("Wake phrase heard → AWAKE")
            self._wake_request = rest  # «Єва, скажи, котра година» → the question goes to the model
            self._wake_phrase = text
            self.state = State.AWAKE

    def _summarize_background(self, text: str) -> str:
        from openai import OpenAI

        from config import OPENAI_API_KEY, OPENAI_LIVE_BACKEND_MODEL

        return make_openai_summarizer(OpenAI(api_key=OPENAI_API_KEY), OPENAI_LIVE_BACKEND_MODEL)(text)

    def _run_awake_session(self) -> None:
        # Token refresh + TLS to Google run while the voice session connects and greets,
        # so the first calendar/mail request of this wake doesn't pay for them.
        threading.Thread(target=self.router.warm_up, daemon=True, name="google-warm-up").start()
        if self._voice_engine == "live":
            self._run_awake_session_live()
        else:
            self._run_awake_session_realtime()


    # ── Live tool handlers (registered on ToolExecutor; no Gmail) ─────────────


    def _web_search_tool_result(
        self,
        args: dict,
        *,
        session_id: str | None = None,
        delegation_id: str | None = None,
    ) -> ToolResult:
        return web_search_tool_result(
            args, session_id=session_id, delegation_id=delegation_id, limiter=self._web_search_limiter
        )

    # ── Tool calls (assistant commands) ───────────────────────────────────────


    def _google_account(self, args: dict) -> AgentResult:
        action = (args.get("action") or "").strip().lower()
        # Ignore any LLM-supplied email/user_sub — never use as identity.
        if action == "switch":
            action = "reauth_switch"  # alias; always a fresh browser login, never an email lookup
        if action in _BROWSER_CONSENT_ACTIONS:
            # The browser step takes minutes: run it in the background so the conversation
            # (including help with Google's consent screen) continues meanwhile.
            return self.router.start_consent(action, on_done=self._announce_consent_result)
        if action == "status":
            return self.router.google_status()
        if action == "disconnect":
            return self.router.disconnect_google()
        if action in ("lock_session", "lock"):
            return self.router.lock_session()
        return AgentResult(
            "needs_more_info",
            "Доступні дії: connect, status, disconnect, grant_all, grant_gmail, grant_notes, "
            "reauth_switch, lock_session.",
        )

    def _announce_consent_result(self, result: AgentResult) -> None:
        """Speak the outcome of a background Google consent into whichever session is open;
        if the user already ended the conversation, say it right after the next wake."""
        message = result.message
        if self._live is not None:
            self._live.speak_context(message)
        elif self.rt is not None:
            self.rt.say(message)
        else:
            self._deferred_announcement = message

    def _pop_deferred_announcement(self) -> str | None:
        message, self._deferred_announcement = self._deferred_announcement, None
        return message


    def _connectivity_watch_loop(self) -> None:
        """Background probe: alert only on hard Google API/network errors, not missing login."""
        while self._running:
            result = _run_connectivity_checks(self.router)
            # auth_required before first login is expected — do not arm spoken alerts.
            ok = result.status in ("success", "auth_required")
            if not ok:
                logger.warning("Background connectivity check failed: status=%s", result.status)
            self._connectivity_watcher.record_check_result(ok, _describe_connection_status(result))
            time.sleep(CONNECTIVITY_CHECK_INTERVAL_S)


    # ── Long-term memory ──────────────────────────────────────────────────────

    def _load_memory(self) -> dict:
        if _MEMORY_FILE.exists():
            try:
                return json.loads(_MEMORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_memory(self) -> None:
        _MEMORY_FILE.write_text(
            json.dumps(self._memory, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _build_instructions(self) -> str:
        prompt = SYSTEM_PROMPT + f" Сьогодні {date.today().isoformat()}."
        lang_code = self._memory.get("language", "uk")
        lang_name = LANGUAGE_OPTIONS.get(lang_code, LANGUAGE_OPTIONS["uk"])
        prompt += f" Спілкуйся виключно {lang_name} мовою."
        if name := self._memory.get("assistant_name"):
            prompt += f" Твоє ім'я — {name}. Представляйся цим ім'ям."
        return prompt

    def _set_assistant_name(self, name: str) -> str:
        self._memory["assistant_name"] = name
        self._save_memory()
        if self.rt is not None:
            self.rt.update_instructions(self._build_instructions())
        if self._live is not None:
            self._live.append_instruction(f"Your name is now {name}. Introduce yourself with that name.")
        return f"Ім'я асистента змінено на {name}."

    def _change_voice(self, voice: str) -> str:
        """Persists the chosen voice and ends this session — the Realtime API
        fixes the output voice for the lifetime of one connection, so there's
        no way to hot-swap it mid-conversation. See _run_awake_session_realtime's
        _voice_change_pending handling for the actual shutdown."""
        if voice not in VOICE_OPTIONS:
            return f"Голос {voice!r} не підтримується — скажи користувачу спробувати ще раз."
        self._memory["realtime_voice"] = voice
        self._save_memory()
        self._sleep_requested = True
        self._history_cutoff = self._pending_cutoff
        self._voice_change_pending = True
        return (
            f"Голос змінено на {voice}. Це набуде чинності лише після перезапуску програми — "
            "коротко повідом користувачу про це й попрощайся."
        )

    def _change_language(self, language: str) -> str:
        """Applies immediately, no restart — unlike voice, both halves of
        "language" (the instructions text and the input transcription hint)
        can be updated live via session.update."""
        if language not in LANGUAGE_OPTIONS:
            return f"Мова {language!r} не підтримується — скажи користувачу спробувати ще раз."
        self._memory["language"] = language
        self._save_memory()
        if self.rt is not None:
            self.rt.update_instructions(self._build_instructions())
            self.rt.update_transcription_language(language)
        if self._live is not None:
            lang_name = LANGUAGE_OPTIONS[language]
            self._live.append_instruction(f"From now on speak exclusively in {lang_name}.")
        return f"Мову змінено на {LANGUAGE_OPTIONS[language]}. Наступну репліку скажи вже цією мовою."

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _has_trigger(self, text: str) -> bool:
        return match_wake(text) is not None

    def _cleanup(self) -> None:
        if self.rt is not None:
            self.rt.close()
        if self._live is not None:
            self._live.close()
        self.tts.cleanup()
        self.stt.close()
        logger.info("Assistant shut down cleanly.")
