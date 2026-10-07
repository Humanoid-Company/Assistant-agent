"""Cheaper pauses: the page closes a paused Live session after 30 s, so a woken Єва lives on the
history — it must hold what tools returned, and the call must stay open while a confirmation or a
tool is in flight."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import server.app as web
from agents.types import AgentResult
from server.live_bridge import SidebandToolBridge
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutor
from voice.conversation import ConversationLog, remember_tool_result

CAROL = "carol-browser-0123456789ab"


H = {"X-Client-Id": CAROL}


class _BusyBridge:
    busy = True


def test_tool_results_stay_in_the_history():
    log = ConversationLog()
    log.add("user", "Що в мене завтра?")
    remember_tool_result(log, "calendar_list_events", "Ось найближчі події: Стендап о 10:00; Обід з Олегом о 13:00")
    remember_tool_result(log, "change_voice", "Голос змінено.")  # session tools carry nothing
    log.add("assistant", "Завтра стендап о десятій і обід з Олегом.")
    texts = [i["content"][0]["text"] for i in log.live_input()]
    assert texts[1] == "(Результат calendar_list_events: Ось найближчі події: Стендап о 10:00; Обід з Олегом о 13:00)"
    assert texts[2] == "Завтра стендап о десятій і обід з Олегом."
    assert not any("change_voice" in t for t in texts)


def test_long_tool_result_is_shortened():
    log = ConversationLog()
    log.add_tool_note("web_search", "слово " * 200)
    text = log.turns()[0].text
    assert len(text) < 450 and text.endswith("…)")


def test_history_keeps_20k_characters():
    log = ConversationLog()
    for i in range(60):
        log.add("user" if i % 2 == 0 else "assistant", f"репліка {i} " + "а" * 300)
        log.end_turn()
    used = sum(len(i["content"][0]["text"]) for i in log.live_input())
    assert 15_000 < used <= 20_000


def test_bridge_puts_tool_result_into_history():
    log = ConversationLog()
    executor = ToolExecutor(calendar=CalendarToolWrappers(lambda **kw: AgentResult("success", "Ось найближчі події: Стендап о 10:00", {})))
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor, conversation=log)
    bridge._connection = SimpleNamespace(
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
    )
    asyncio.run(bridge._execute_and_continue(
        name="calendar_list_events", arguments={}, call_id="c1", delegation_id=None, key="",
    ))
    assert "Стендап о 10:00" in log.turns()[-1].text


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    return TestClient(web.app)


def test_paused_session_is_kept_while_a_tool_runs(client):
    user = web.users.get(CAROL)
    user.bridges.clear()
    assert client.get("/api/me", headers=H).json()["keep_session"] is False
    user.bridges.add(_BusyBridge())
    assert client.get("/api/me", headers=H).json()["keep_session"] is True
    user.bridges.clear()


def test_paused_session_is_kept_while_a_confirmation_waits(client, monkeypatch):
    user = web.users.get(CAROL)
    user.bridges.clear()
    monkeypatch.setattr(user.router.accounts, "active_sub", lambda: "sub-carol")
    monkeypatch.setattr(user.router.pending, "has_pending", lambda sub: sub == "sub-carol")
    assert client.get("/api/me", headers=H).json()["keep_session"] is True
