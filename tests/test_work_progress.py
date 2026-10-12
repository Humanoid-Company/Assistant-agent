"""Delegated work (search, calendar, mail) runs while she is silent: after a few quiet seconds the
sideband has her say she is on it («Так, щось уже знаходжу…»), and stops once the answer comes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agents.types import AgentResult
from prompts.live_prompt import work_progress_note
from server import live_bridge
from server.live_bridge import SidebandToolBridge
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutor


def _event(inner: dict) -> dict:
    return {"type": "response.event", "delegation_id": "dlg-1", "event": inner}


def _call(call_id: str) -> dict:
    return _event({
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": call_id, "name": "calendar_list_events", "arguments": "{}"},
    })


def _bridge(monkeypatch, *, after=(0.05, 0.15), quiet=0.0) -> tuple[SidebandToolBridge, SimpleNamespace]:
    monkeypatch.setattr(live_bridge, "_PROGRESS_AFTER_S", after)
    monkeypatch.setattr(live_bridge, "_PROGRESS_QUIET_S", quiet)
    executor = ToolExecutor(calendar=CalendarToolWrappers(lambda **kw: AgentResult("success", "ok", {})))
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    conn = SimpleNamespace(
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
        session=SimpleNamespace(commentary=SimpleNamespace(append=AsyncMock())),
    )
    bridge._connection = conn
    return bridge, conn


def _said(conn: SimpleNamespace) -> list[str]:
    return [c.kwargs["content"] for c in conn.session.commentary.append.await_args_list]


def test_note_names_the_work_and_never_a_result():
    first = work_progress_note(["web_search"])
    assert "the web search is still running" in first and "Never give or guess a result" in first
    assert "longer than usual" in work_progress_note(["calendar_list_events"], again=True)
    assert "the task is still running" in work_progress_note([])


def test_long_work_gets_two_progress_lines(monkeypatch):
    bridge, conn = _bridge(monkeypatch)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_call("c1"))
        await bridge._handle_event(_event({"type": "response.completed", "response": {"id": "r1"}}))
        await asyncio.sleep(0.9)  # the continuation is still writing the answer (checks every 0.25 s)
        said = _said(conn)
        assert len(said) == 2
        assert "the calendar lookup" in said[0] and "longer than usual" in said[1]

    asyncio.run(run())


def test_final_answer_stops_progress(monkeypatch):
    bridge, conn = _bridge(monkeypatch, after=(0.2, 0.4))

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_event({"type": "response.completed", "response": {"id": "r1"}}))
        await asyncio.sleep(0.5)
        assert _said(conn) == [] and not bridge._work and not bridge._progress

    asyncio.run(run())


def test_no_progress_line_while_someone_talks(monkeypatch):
    bridge, conn = _bridge(monkeypatch, quiet=10.0)

    async def run():
        await bridge._handle_event({"type": "session.input_transcript.delta", "delta": "а ще"})
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await asyncio.sleep(0.4)
        assert _said(conn) == []

    asyncio.run(run())
