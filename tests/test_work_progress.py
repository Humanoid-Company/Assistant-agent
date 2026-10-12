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


def _bridge(monkeypatch, *, after=0.05, quiet=0.0) -> tuple[SidebandToolBridge, SimpleNamespace]:
    monkeypatch.setattr(live_bridge, "_PROGRESS_AFTER_S", after)
    monkeypatch.setattr(live_bridge, "_PROGRESS_QUIET_S", quiet)
    monkeypatch.setattr(live_bridge, "_PROGRESS_INPUT_GRACE_S", 0.0)
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


def test_note_names_the_work_without_sample_lines():
    note = work_progress_note(["web_search"])
    assert "the web search for their last request is still running" in note and "Not a result" in note
    assert "«" not in note  # sample lines were said word for word
    assert "nobody is talking" not in note and "go quiet" not in note  # a choice → a literal «null»
    assert "the task" in work_progress_note([])


def test_long_work_gets_one_progress_line(monkeypatch):
    bridge, conn = _bridge(monkeypatch)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_call("c1"))
        await asyncio.sleep(0.6)  # the tool still runs (checks every 0.25 s)
        said = _said(conn)
        assert len(said) == 1 and "the calendar lookup" in said[0]

    asyncio.run(run())


def test_no_progress_once_the_results_are_in(monkeypatch):
    bridge, conn = _bridge(monkeypatch, after=0.3)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_call("c1"))
        await bridge._handle_event(_event({"type": "response.completed", "response": {"id": "r1"}}))
        await asyncio.gather(*bridge._tool_tasks)  # results in → continued: the answer is being written
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r2"}}))
        await asyncio.sleep(0.6)
        assert _said(conn) == [] and "dlg-1" in bridge._answering

    asyncio.run(run())


def test_final_answer_stops_progress(monkeypatch):
    bridge, conn = _bridge(monkeypatch, after=0.2)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(_event({"type": "response.completed", "response": {"id": "r1"}}))
        await asyncio.sleep(0.5)
        assert _said(conn) == [] and not bridge._work and not bridge._progress

    asyncio.run(run())


def test_no_progress_after_the_user_speaks_again(monkeypatch):
    bridge, conn = _bridge(monkeypatch, after=0.3)

    async def run():
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await asyncio.sleep(0.05)
        await bridge._handle_event({"type": "session.input_transcript.delta", "delta": "ні, я не про це"})
        await asyncio.sleep(0.6)
        assert _said(conn) == []

    asyncio.run(run())


def test_no_progress_while_she_talks(monkeypatch):
    bridge, conn = _bridge(monkeypatch, quiet=10.0)

    async def run():
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": "Секунду."})
        await bridge._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await asyncio.sleep(0.4)
        assert _said(conn) == []

    asyncio.run(run())
