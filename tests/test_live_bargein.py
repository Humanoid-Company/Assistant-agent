"""GPT-Live barge-in + non-blocking tool execution regressions."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.types import AgentResult
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.gmail_tools import GmailToolWrappers
from tools.results import ToolResult
from tools.task_context import TaskRevisionTracker
from voice.live_session import LiveVoiceSession
from voice.playback import PlaybackTracker


def _executor_with_handler(name: str, handler, *, run_in_thread: bool) -> ToolExecutor:
    cal = CalendarToolWrappers(lambda **kw: AgentResult("success", "ok"))
    mail = GmailToolWrappers(lambda **kw: AgentResult("success", "ok"))
    ex = ToolExecutor(calendar=cal, gmail=mail, revisions=TaskRevisionTracker())
    ex.register(name, handler, run_in_thread=run_in_thread)
    return ex


def test_playback_barge_in_on_input_transcript_delta():
    ex = _executor_with_handler(
        "noop",
        lambda a, c: ToolResult(ok=True, status="ok", message=""),
        run_in_thread=False,
    )
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-barge")
    # Avoid opening PortAudio — seed queue state only.
    session.player._stream = MagicMock()
    session.player._thread = MagicMock()
    session.player.enqueue(b"\x00\x01" * 200)
    assert session.player.is_playing
    assert session.player.queued_bytes > 0

    asyncio.run(
        session._handle_event({"type": "session.input_transcript.delta", "delta": "стоп"})
    )
    assert session.player.queued_bytes == 0


def test_barge_in_does_not_cancel_pending_gmail(tmp_path):
    from tests.helpers_google import build_test_router

    router, _cal, mail, *_ = build_test_router(tmp_path)
    prep = router.gmail_action(
        action="send",
        to="a@example.com",
        subject="Hi",
        body="Body",
        session_id="sess-a",
    )
    assert prep.status == "confirmation_required"
    op_id = prep.data["op_id"]

    ex = ToolExecutor(
        calendar=CalendarToolWrappers(router.calendar_action),
        gmail=GmailToolWrappers(router.gmail_action),
    )
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-a")
    session.player._stream = MagicMock()
    session.player._thread = MagicMock()
    session.player.enqueue(b"\x00\x01" * 50)
    asyncio.run(
        session._handle_event({"type": "session.input_transcript.delta", "delta": "але"})
    )
    # Pending op must still be confirmable — barge-in is playback-only.
    done = router.gmail_action(
        action="confirm", confirmation="yes", op_id=op_id, session_id="sess-a"
    )
    assert done.status == "success"
    assert len(mail.sent) == 1


def test_blocking_tool_does_not_freeze_microphone_sender():
    started = threading.Event()
    release = threading.Event()

    def slow_handler(args, context):
        started.set()
        release.wait(timeout=2)
        return ToolResult(ok=True, status="ok", message="done")

    ex = _executor_with_handler("slow_tool", slow_handler, run_in_thread=True)
    assert ex.is_offloaded("slow_tool")

    sent_chunks: list[str] = []

    class FakeInputAudio:
        async def append(self, *, audio: str, event_id=None):
            sent_chunks.append(audio)

    class FakeSession:
        input_audio = FakeInputAudio()

        async def start(self, **kwargs):
            return None

        async def close(self):
            return None

    session = LiveVoiceSession(tool_executor=ex, session_id="sess-mic")
    session._connection = SimpleNamespace(session=FakeSession())
    session._mic_read = lambda: b"\x00\x01" * 64
    session._sleep_requested = False

    async def _run():
        sender = asyncio.create_task(session._send_audio_loop())
        tool_task = asyncio.create_task(
            session._executor.execute(
                "slow_tool", {}, ToolExecutionContext(session_id="sess-mic")
            )
        )
        await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.25)
        assert len(sent_chunks) >= 1
        release.set()
        await tool_task
        session._sleep_requested = True
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)

    asyncio.run(_run())


def test_blocking_tool_does_not_freeze_event_processing():
    release = threading.Event()

    def slow_handler(args, context):
        release.wait(timeout=2)
        return ToolResult(ok=True, status="ok", message="done")

    ex = _executor_with_handler("slow_tool", slow_handler, run_in_thread=True)
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-evt")
    session.player._stream = MagicMock()
    session.player._thread = MagicMock()
    session.player.enqueue(b"\x00\x01" * 80)

    async def _run():
        tool_task = asyncio.create_task(
            session._executor.execute("slow_tool", {}, ToolExecutionContext())
        )
        await asyncio.sleep(0.05)
        await session._handle_event(
            {"type": "session.input_transcript.delta", "delta": "стоп"}
        )
        assert session.player.queued_bytes == 0
        release.set()
        await tool_task

    asyncio.run(_run())


def test_google_account_executes_off_event_loop_thread():
    loop_thread_id = {"id": None}
    handler_thread_id = {"id": None}

    def handler(args, context):
        handler_thread_id["id"] = threading.get_ident()
        time.sleep(0.05)
        return ToolResult(
            ok=True,
            status="ok",
            message="permission_granted",
            data={"permission_granted": True},
        )

    ex = _executor_with_handler("google_account", handler, run_in_thread=True)
    assert ex.is_offloaded("google_account")

    async def _run():
        loop_thread_id["id"] = threading.get_ident()
        result = await ex.execute(
            "google_account",
            {"action": "grant_gmail"},
            ToolExecutionContext(),
        )
        assert result.ok
        assert result.data.get("permission_granted") is True

    asyncio.run(_run())
    assert handler_thread_id["id"] is not None
    assert handler_thread_id["id"] != loop_thread_id["id"]


def test_calendar_gmail_wrappers_are_offloaded_by_default():
    ex = _executor_with_handler(
        "google_account",
        lambda a, c: ToolResult(ok=True, status="ok", message="x"),
        run_in_thread=True,
    )
    assert ex.is_offloaded("google_account")
    assert ex.is_offloaded("gmail_search_messages")
    assert ex.is_offloaded("gmail_prepare_send")
    assert ex.is_offloaded("calendar_prepare_create")
    assert ex.is_offloaded("calendar_list_events")


def test_mic_send_loop_not_gated_on_playback():
    live_src = Path(__file__).resolve().parents[1] / "voice" / "live_session.py"
    text = live_src.read_text(encoding="utf-8")
    send_section = text.split("async def _send_audio_loop")[1].split("async def _handle_event")[0]
    assert "player.is_playing" not in send_section
    assert "don't send microphone" not in send_section.lower()

    tracker = PlaybackTracker(sample_rate=24000)
    tracker._stream = MagicMock()
    tracker._thread = MagicMock()
    tracker.enqueue(b"\x00\x01" * 10)
    assert tracker.is_playing
