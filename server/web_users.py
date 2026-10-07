"""Per-browser state for the hosted version.

Each browser gets a random client id (kept in its localStorage). Everything Google-related is
isolated per client id: its own AgentRouter, token store and active account, so two
teammates testing at once never see each other's mail or calendar.

State lives in memory only: a server restart (Render redeploy / free-tier sleep) means
users connect Google again. Fine for team testing; use a database for anything longer.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.types import AgentResult
from auth.token_store import InMemoryTokenStore
from integrations.web_search import WebSearchRateLimiter
from router.agent_router import AgentRouter
from router.factory import build_agent_router
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.gmail_tools import GmailToolWrappers
from tools.notes_tools import NotesToolWrappers
from tools.results import ToolResult, agent_result_to_tool_result
from tools.task_context import TaskRevisionTracker
from tools.web_search_tool import web_search_tool_result
from voice.background import BackgroundLog, make_openai_summarizer
from voice.conversation import ConversationLog
from voice.options import (
    LANGUAGE_OPTIONS,
    SPEED_OPTIONS,
    STYLE_OPTIONS,
    VOICE_PERSONAS,
    VOICE_REQUEST_RE,
    _sanitize_name,
    asked_for_voice_change,
    delivery_instruction,
    voice_request_target,
)

logger = logging.getLogger(__name__)

_STATE_DIR = Path(tempfile.gettempdir()) / "voice-agent-web"
_MAX_USERS = 500
_IDLE_EVICT_S = 24 * 3600
_background: set[asyncio.Task] = set()
VOICE_SWITCH_DEDUP_S = 15.0

_CONNECT_ON_PAGE = (
    "Щоб підключити Google, натисніть на сторінці кнопку «Підключити Google». Відкриється вікно "
    "Google: оберіть акаунт і поставте всі галочки — календар, пошта і Google Drive. "
    "Я скажу, щойно вхід завершиться."
)


@dataclass
class WebUser:
    client_id: str
    router: AgentRouter
    executor: ToolExecutor = field(init=False)
    assistant_name: str | None = None
    language: str = "uk"
    voice: str | None = None
    speed: str = "normal"
    style: str = "normal"
    # The voice was changed during a call: the page reconnects with the new voice and the same
    # conversation once the current session closes.
    reconnect_pending: bool = False
    # Several paths can act on one «зміни голос …» (browser recogniser, transcript, the model):
    # within this window after a switch the others are the same request, not a new one.
    voice_switched_at: float = 0.0
    voice_switch_note: str = ""  # who switched it — shown on the page next to «Голос змінено»
    # Dialogue history independent of the Live session (and so of the voice).
    conversation: ConversationLog = field(default_factory=ConversationLog)
    # What people nearby said while Єва was paused — handed to her on the next wake.
    background: BackgroundLog = field(default_factory=lambda: BackgroundLog(_summarize_background))
    last_seen: float = field(default_factory=time.time)
    bridges: set[Any] = field(default_factory=set)  # live SidebandToolBridge objects

    def switch_voice_by_request(self, utterance: str) -> bool:
        """«Єва, зміни голос на …» heard in the transcript: switch now (the page reconnects)."""
        if time.time() - self.voice_switched_at < VOICE_SWITCH_DEDUP_S:
            return False
        voice = voice_request_target(utterance, self.voice)
        if voice is None or voice == self.voice:
            return False
        # The matched words are logged so a voice switch nobody asked for can be traced.
        match = VOICE_REQUEST_RE.search(utterance)
        heard = utterance[match.start(): match.end() + 30] if match else ""
        logger.info("web.voice.by_request %s → %s heard=%r", self.voice, voice, heard)
        self.voice_switch_note = f"почула: «{heard.strip()}»"
        self.voice = voice
        self.reconnect_pending = True
        self.voice_switched_at = time.time()
        return True

    async def apply_delivery(self) -> None:
        """Push the current speed/style into the running call."""
        text = delivery_instruction(self.speed, self.style, changed=True)
        for bridge in list(self.bridges):
            try:
                await bridge.append_instruction(text)
            except Exception:
                logger.debug("apply_delivery failed", exc_info=True)

    def __post_init__(self) -> None:
        self.executor = _build_executor(self)


_summarizer = None


def _summarize_background(text: str) -> str:
    """Compress a long pause's overheard talk (lazy: one OpenAI client for the whole server)."""
    global _summarizer
    if _summarizer is None:
        from openai import OpenAI

        from config import OPENAI_API_KEY, OPENAI_LIVE_BACKEND_MODEL

        _summarizer = make_openai_summarizer(OpenAI(api_key=OPENAI_API_KEY), OPENAI_LIVE_BACKEND_MODEL)
    return _summarizer(text)


def _build_executor(user: WebUser) -> ToolExecutor:
    router = user.router
    executor = ToolExecutor(
        calendar=CalendarToolWrappers(router.calendar_action),
        gmail=GmailToolWrappers(router.gmail_action),
        notes=NotesToolWrappers(router.notes_action),
        revisions=TaskRevisionTracker(),
    )
    limiter = WebSearchRateLimiter()

    def google_account(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        if action == "status":
            return agent_result_to_tool_result(router.google_status())
        if action == "disconnect":
            return agent_result_to_tool_result(router.disconnect_google())
        if action in ("lock_session", "lock"):
            return agent_result_to_tool_result(router.lock_session())
        # connect / switch / grant_*: on the web the login is a button on the page (the server
        # can't open a browser for the user).
        return agent_result_to_tool_result(
            AgentResult("needs_more_info", _CONNECT_ON_PAGE, {"connect_via_page_button": True})
        )

    def web_search(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        return web_search_tool_result(
            args, session_id=ctx.session_id, delegation_id=ctx.delegation_id, limiter=limiter
        )

    def check_connection(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        return agent_result_to_tool_result(router.check_connection())

    def set_name(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        name = _sanitize_name(str(args.get("name") or ""))
        if not name:
            return ToolResult(ok=False, status="needs_more_info", message="Не зрозумів нового імені.")
        user.assistant_name = name
        return ToolResult(ok=True, status="ok", message=f"Тепер мене звати {name}.")

    def change_language(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        language = str(args.get("language") or "").strip().lower()
        if language not in LANGUAGE_OPTIONS:
            return ToolResult(ok=False, status="needs_more_info", message="Доступні мови: uk, ru, en.")
        user.language = language
        return ToolResult(ok=True, status="ok", message=f"Далі говорю {LANGUAGE_OPTIONS[language]}.")

    def change_voice(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        voice = str(args.get("voice") or "").strip().lower()
        # Only the voices offered in the page's picker, so the picker can always show the choice.
        if voice not in VOICE_PERSONAS:
            names = ", ".join(f"{p.label} ({p.voice})" for p in VOICE_PERSONAS.values())
            return ToolResult(ok=False, status="needs_more_info", message=f"Такого голосу немає. Доступні: {names}.")
        if voice == user.voice or time.time() - user.voice_switched_at < VOICE_SWITCH_DEDUP_S:
            return ToolResult(ok=True, status="ok", message="Голос уже змінено. Нічого про це не кажи.")
        last = (ctx.user_utterances or [""])[-1][-60:]
        if not asked_for_voice_change(ctx.user_utterances):
            logger.info("web.voice.tool_refused voice=%s (user didn't mention the voice)", voice)
            return ToolResult(
                ok=False,
                status="error",
                message="Користувач не просив змінити голос — не змінюй його, просто продовжуй розмову.",
            )
        logger.info("web.voice.by_tool %s → %s last_user=%r", user.voice, voice, last)
        user.voice_switch_note = f"попросила модель; останнє від вас: «{last}»"
        user.voice = voice
        user.reconnect_pending = True
        user.voice_switched_at = time.time()
        for bridge in list(user.bridges):
            bridge.restart_for_voice = True  # the page reconnects with the new voice and the same history
        return ToolResult(
            ok=True,
            status="ok",
            message="Голос змінено. Нічого про це не кажи — розмова одразу продовжиться новим голосом.",
        )

    def set_voice_style(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        speed = str(args.get("speed") or user.speed).strip().lower()
        style = str(args.get("style") or user.style).strip().lower()
        if speed not in SPEED_OPTIONS or style not in STYLE_OPTIONS:
            return ToolResult(ok=False, status="needs_more_info", message="Темп: slow/normal/fast, стиль: calm/normal/expressive.")
        user.speed, user.style = speed, style
        task = asyncio.get_running_loop().create_task(user.apply_delivery())
        _background.add(task)
        task.add_done_callback(_background.discard)
        return ToolResult(ok=True, status="ok", message="Готово, говорю так з наступного речення.")

    def end_conversation(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        return ToolResult(ok=True, status="ok", message="Попрощайся коротко.")

    executor.register("google_account", google_account, run_in_thread=True)
    executor.register("web_search", web_search, run_in_thread=True)
    executor.register("check_connection", check_connection, run_in_thread=True)
    executor.register("set_assistant_name", set_name)
    executor.register("change_language", change_language)
    executor.register("change_voice", change_voice)
    executor.register("set_voice_style", set_voice_style)
    executor.register("end_conversation", end_conversation)
    return executor


class WebUserRegistry:
    def __init__(self, *, client_secrets_file: Path | str, timezone: str) -> None:
        self._users: dict[str, WebUser] = {}
        self._lock = threading.Lock()
        self._client_secrets_file = client_secrets_file
        self._timezone = timezone
        _STATE_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, client_id: str) -> WebUser:
        with self._lock:
            user = self._users.get(client_id)
            if user is None:
                self._evict_idle()
                router = build_agent_router(
                    client_secrets_file=self._client_secrets_file,
                    state_file=_STATE_DIR / f"{client_id}.json",
                    timezone=self._timezone,
                    token_store=InMemoryTokenStore(),
                    shared_device=False,
                )
                user = WebUser(client_id=client_id, router=router)
                self._users[client_id] = user
                logger.info("web.user.created users=%s", len(self._users))
            user.last_seen = time.time()
            return user

    def _evict_idle(self) -> None:
        now = time.time()
        stale = [cid for cid, u in self._users.items() if now - u.last_seen > _IDLE_EVICT_S and not u.bridges]
        for cid in stale:
            del self._users[cid]
        while len(self._users) >= _MAX_USERS:
            oldest = min(self._users.values(), key=lambda u: u.last_seen)
            del self._users[oldest.client_id]
