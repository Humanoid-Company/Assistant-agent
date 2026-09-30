"""Engine selection and Live architecture unit tests (no audio hardware)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.types import AgentResult
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.results import agent_result_to_tool_result
from tools.task_context import TaskRevisionTracker
from voice.delegation import accumulate_transcript, extract_completed_function_call
from voice.factory import normalize_voice_engine
from voice.playback import PlaybackTracker


def test_normalize_voice_engine_defaults_and_aliases():
    assert normalize_voice_engine("realtime") == "realtime"
    assert normalize_voice_engine("LIVE") == "live"
    assert normalize_voice_engine("gpt-live") == "live"
    assert normalize_voice_engine("weird") == "realtime"
    assert normalize_voice_engine(None) in ("live", "realtime")


def test_create_voice_session_selects_engines():
    from voice.factory import create_voice_session
    from voice.live_session import LiveVoiceSession
    from voice.realtime_legacy import RealtimeLegacySession

    live = create_voice_session(
        "live",
        tool_executor=ToolExecutor(calendar=CalendarToolWrappers(lambda **kw: AgentResult("success", "x"))),
    )
    assert isinstance(live, LiveVoiceSession)

    rt = create_voice_session(
        "realtime",
        tools=[],
        on_tool_call=lambda *a, **k: None,
        voice="marin",
    )
    assert isinstance(rt, RealtimeLegacySession)


def test_tool_executor_routing_and_errors():
    calls = []

    def cal(**kwargs):
        calls.append(kwargs)
        return AgentResult("confirmation_required", "Підтвердити?", {"op_id": "op-1"})

    ex = ToolExecutor(calendar=CalendarToolWrappers(cal))
    ctx = ToolExecutionContext(session_id="sess")
    result = ex.execute_sync("calendar_prepare_create", {"title": "Обід", "date": "2026-10-01", "time": "15:00"}, ctx)
    assert result.status == "confirmation_required"
    assert result.op_id == "op-1"
    assert calls[0]["action"] == "create"
    assert calls[0]["session_id"] == "sess"

    bad = ex.execute_sync("calendar_prepare_create", "{not-json", ctx)
    assert bad.status == "error"

    unknown = ex.execute_sync("totally_unknown_tool", {}, ctx)
    assert unknown.status == "error"
    assert "Невідома" in unknown.message


def test_tool_executor_exception_not_fake_success():
    def boom(**kwargs):
        raise RuntimeError("google down")

    ex = ToolExecutor(calendar=CalendarToolWrappers(boom))
    result = ex.execute_sync("calendar_list_events", {}, ToolExecutionContext())
    assert result.ok is False
    assert result.status == "error"


def test_stale_revision_blocks_confirm():
    ops = {"n": 0}

    def cal(**kwargs):
        action = kwargs.get("action")
        if action == "create":
            ops["n"] += 1
            op_id = f"op-{ops['n']}"
            return AgentResult("confirmation_required", "ok?", {"op_id": op_id})
        if action == "confirm":
            ops["confirmed"] = kwargs.get("op_id")
            return AgentResult("success", "done", {"op_id": kwargs.get("op_id")})
        return AgentResult("error", "unexpected")

    revs = TaskRevisionTracker()
    ex = ToolExecutor(calendar=CalendarToolWrappers(cal), revisions=revs)
    ctx = ToolExecutionContext(session_id="s")
    prep1 = ex.execute_sync(
        "calendar_prepare_create", {"title": "A", "date": "2026-10-01", "time": "10:00"}, ctx
    )
    assert prep1.op_id == "op-1"
    prep2 = ex.execute_sync(
        "calendar_prepare_create", {"title": "B", "date": "2026-10-02", "time": "11:00"}, ctx
    )
    assert prep2.op_id == "op-2"
    assert not revs.is_op_current("op-1")
    assert revs.is_op_current("op-2")
    stale = ex.execute_sync("calendar_confirm_operation", {"op_id": "op-1"}, ctx)
    assert stale.status == "stale"
    assert "confirmed" not in ops
    ok = ex.execute_sync("calendar_confirm_operation", {"op_id": "op-2"}, ctx)
    assert ok.status in ("ok", "completed", "success") or ok.ok
    assert ops.get("confirmed") == "op-2"


def test_extract_function_call_only_on_output_item_done():
    delta = {
        "type": "response.event",
        "delegation_id": "dlg-1",
        "event": {
            "type": "response.function_call_arguments.delta",
            "delta": '{"title":',
        },
    }
    assert extract_completed_function_call(delta) is None

    done = {
        "type": "response.event",
        "delegation_id": "dlg-1",
        "event": {
            "type": "response.output_item.done",
            "response_id": "resp-1",
            "item": {
                "type": "function_call",
                "call_id": "call-9",
                "name": "calendar_prepare_create",
                "arguments": '{"title":"Обід"}',
            },
        },
    }
    completed = extract_completed_function_call(done)
    assert completed is not None
    assert completed.call_id == "call-9"
    assert completed.name == "calendar_prepare_create"
    assert completed.delegation_id == "dlg-1"
    assert completed.response_id == "resp-1"


def test_transcript_fragments_accumulate():
    buf = ""
    for frag in ("I", " want", " Friday"):
        buf = accumulate_transcript(buf, frag)
    assert buf == "I want Friday"


def test_playback_tracker_enqueue_interrupt_without_hardware():
    tracker = PlaybackTracker(sample_rate=24000)
    # Avoid opening PortAudio in CI — patch start/write loop.
    tracker._stream = MagicMock()
    tracker._thread = threading_dummy()
    tracker.enqueue(b"\x00\x01" * 10)
    assert tracker.queued_bytes > 0
    tracker.interrupt()
    assert tracker.queued_bytes == 0
    tracker.close()


def threading_dummy():
    class T:
        def join(self, timeout=None):
            return None

    return T()


def test_function_result_continues_backend_exactly_once():
    """function_call → ToolExecutor → function_call_output → response.create once."""
    from voice.live_session import LiveVoiceSession

    cal = CalendarToolWrappers(lambda **kw: AgentResult("success", "listed", {}))
    ex = ToolExecutor(calendar=cal)
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-test")
    conn = SimpleNamespace(
        response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()),
            create=AsyncMock(),
        )
    )
    session._connection = conn

    async def _run():
        await session._execute_and_continue(
            name="calendar_list_events",
            arguments={},
            call_id="call-1",
            delegation_id="dlg-1",
            response_id="resp-1",
        )

    asyncio.run(_run())
    conn.response.item.create.assert_awaited_once()
    item = conn.response.item.create.await_args.kwargs["item"]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "call-1"
    conn.response.create.assert_awaited_once()


def test_live_backend_tools_include_gmail_structured_not_gmail_action():
    from tools.live_schemas import LIVE_BACKEND_TOOLS

    names = {t["name"] for t in LIVE_BACKEND_TOOLS}
    assert "gmail_action" not in names
    assert "note_emotion" not in names
    assert "dispatch_task" not in names
    assert "calendar_prepare_create" in names
    assert "calendar_confirm_operation" in names
    assert "gmail_search_messages" in names
    assert "gmail_read_message" in names
    assert "gmail_create_draft" in names
    assert "gmail_prepare_send" in names
    assert "gmail_prepare_reply" in names
    assert "gmail_confirm_send" in names
    assert "gmail_reject_send" in names


def test_agent_result_to_tool_result_preserves_op_id():
    tr = agent_result_to_tool_result(
        AgentResult("confirmation_required", "Підтвердити?", {"op_id": "abc"})
    )
    assert tr.ok is True
    assert tr.status == "confirmation_required"
    assert tr.op_id == "abc"
    blob = tr.to_dict()
    assert blob["op_id"] == "abc"
