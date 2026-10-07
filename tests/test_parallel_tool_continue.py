"""Parallel tool calls: continue the delegated response only once it has finished and every call
it made has a result. A fast first tool must not trigger response.create while the model is still
emitting the second call («Missing tool results or approvals for: call_…»)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agents.types import AgentResult
from server import live_bridge
from server.live_bridge import SidebandToolBridge
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutor
from voice.delegation import DelegatedResponseTracker


def test_tracker_waits_for_the_response_and_all_results():
    t = DelegatedResponseTracker()
    t.response_started("d1")
    t.call_started("d1", "c1")
    assert t.call_finished("d1", "c1") is False  # the response may still emit more calls
    t.call_started("d1", "c2")
    assert t.response_finished("d1") is False  # c2 still running
    assert t.call_finished("d1", "c2") is True
    t.mark_continued("d1")
    assert t.call_finished("d1", "c2") is False  # never twice


def test_tracker_results_before_finish_continue_on_finish():
    t = DelegatedResponseTracker()
    t.response_started("d1")
    t.call_started("d1", "c1")
    assert t.call_finished("d1", "c1") is False
    assert t.results_complete("d1") is True
    assert t.response_finished("d1") is True


def test_tracker_failed_response_is_not_continued():
    t = DelegatedResponseTracker()
    t.response_started("d1")
    t.call_started("d1", "c1")
    assert t.response_finished("d1", ok=False) is False
    assert t.call_finished("d1", "c1") is False
    assert t.results_complete("d1") is False


def test_untracked_call_continues_right_away():
    assert DelegatedResponseTracker().call_finished("x", "c1") is True


def _event(inner: dict) -> dict:
    return {"type": "response.event", "delegation_id": "dlg-1", "event": inner}


def _call(call_id: str) -> dict:
    return _event({
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": call_id, "name": "calendar_list_events", "arguments": "{}"},
    })


def test_bridge_fast_first_tool_does_not_continue_before_second_call():
    executor = ToolExecutor(calendar=CalendarToolWrappers(lambda **kw: AgentResult("success", "ok", {})))
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    conn = SimpleNamespace(
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
    )
    bridge._connection = conn

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "resp-1"}}))
        await bridge._handle_event(_call("c1"))
        await asyncio.gather(*bridge._tool_tasks)  # c1 finishes before the model emits c2
        assert conn.response.create.await_count == 0
        await bridge._handle_event(_call("c2"))
        await bridge._handle_event(_event({"type": "response.completed", "response": {"id": "resp-1"}}))
        await asyncio.gather(*bridge._tool_tasks)
        assert conn.response.item.create.await_count == 2
        assert conn.response.create.await_count == 1

    asyncio.run(run())


def test_bridge_continues_without_finish_event_after_fallback(monkeypatch):
    monkeypatch.setattr(live_bridge, "_CONTINUE_FALLBACK_S", 0.01)
    executor = ToolExecutor(calendar=CalendarToolWrappers(lambda **kw: AgentResult("success", "ok", {})))
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    conn = SimpleNamespace(
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
    )
    bridge._connection = conn

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "resp-1"}}))
        await bridge._handle_event(_call("c1"))
        for _ in range(3):  # the tool task, then the fallback it schedules
            await asyncio.gather(*list(bridge._tool_tasks))
        assert conn.response.create.await_count == 1

    asyncio.run(run())
