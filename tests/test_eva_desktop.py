"""Desktop Єва: «Дякую, Єва» pauses without losing the conversation; wakes carry the request."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from agents.types import AgentResult
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutor
from tools.gmail_tools import GmailToolWrappers
from tools.task_context import TaskRevisionTracker
from voice.conversation import ConversationLog
from voice.live_session import LiveVoiceSession


def _session(log: ConversationLog, voice: str = "gleam") -> LiveVoiceSession:
    ex = ToolExecutor(
        calendar=CalendarToolWrappers(lambda **kw: AgentResult("success", "ok")),
        gmail=GmailToolWrappers(lambda **kw: AgentResult("success", "ok")),
        revisions=TaskRevisionTracker(),
    )
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-eva", voice=voice, conversation=log)
    session.player._stream = MagicMock()  # no PortAudio in tests
    session.player._thread = MagicMock()
    return session


def _feed(session: LiveVoiceSession, *events: tuple[str, str]) -> None:
    async def run():
        for etype, delta in events:
            await session._handle_event({"type": etype, "delta": delta})

    asyncio.run(run())


def test_stop_phrase_pauses_even_while_speaking_and_keeps_history():
    log = ConversationLog()
    session = _session(log)
    _feed(
        session,
        ("session.input_transcript.delta", "Мене звати Остап."),
        ("session.output_transcript.delta", "Приємно, Остапе! Ну що, розкажу про погоду: завтра"),
    )
    session.player.enqueue(b"\x00\x01" * 400)  # Єва is talking
    assert session.player.is_playing
    _feed(session, ("session.input_transcript.delta", "Дякую, "), ("session.input_transcript.delta", "Єво"))
    assert session.pause_requested and session.sleep_requested
    assert session.player.queued_bytes == 0  # speech cut immediately
    assert [t.text for t in log.turns()][0] == "Мене звати Остап."


def test_next_session_starts_from_the_shared_history():
    log = ConversationLog()
    _feed(
        _session(log),
        ("session.input_transcript.delta", "Я п'ю каву без цукру."),
        ("session.output_transcript.delta", "Запам'ятала!"),
    )
    config = _session(log, voice="meridian")._session_config()  # e.g. after a voice change
    assert config["audio"]["output"]["voice"] == "meridian"
    assert [i["content"][0]["text"] for i in config["input"]] == ["Я п'ю каву без цукру.", "Запам'ятала!"]


def test_wake_phrase_wakes_and_carries_the_request():
    from assistant import Assistant
    from voice.base import State

    bot = Assistant.__new__(Assistant)  # no microphone / Google in tests
    bot.state = State.SLEEPING
    bot._wake_request = ""
    bot.stt = MagicMock()
    bot.stt.listen.return_value = ("Єво, скажи, котра година", None, "")
    bot._handle_sleeping()
    assert bot.state == State.AWAKE and bot._wake_request == "котра година"

    bot.state = State.SLEEPING
    bot.stt.listen.return_value = ("привіт", None, "")
    bot._handle_sleeping()
    assert bot.state == State.SLEEPING


def test_voice_request_in_transcript_switches_without_the_model(monkeypatch):
    log = ConversationLog()
    asked: list[str] = []
    session = _session(log)
    session._on_voice_request = lambda text: asked.append(text) or True

    async def run():
        for delta in ["Єва, зміни голос ", "на чоловічий"]:
            await session._handle_event({"type": "session.input_transcript.delta", "delta": delta})
        await asyncio.sleep(1.2)  # the request ends after a short pause

    asyncio.run(run())
    assert asked == ["Єва, зміни голос на чоловічий"]
