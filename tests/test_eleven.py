"""ElevenLabs test engine: TTS proxy settings, the text-mode prompt, the Realtime call and its tools."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server.app as web
from prompts.live_prompt import LIVE_PROMPT, build_eleven_prompt
from server.eleven import ELEVEN_TOOLS, RealtimeToolBridge, TtsRequest, history_items, tts_payload
from tools.results import ToolResult
from voice.conversation import ConversationLog

HEADERS = {"X-Client-Id": "eleven-browser-0123456789"}


def test_tts_payload_keeps_settings_in_range():
    payload = tts_payload(TtsRequest(text=" Привіт. ", voice_id="v", stability=3, similarity=-1, speed=2, style=0.4))
    assert payload["text"] == "Привіт."
    assert payload["voice_settings"]["stability"] == 1.0
    assert payload["voice_settings"]["similarity_boost"] == 0.0
    assert payload["voice_settings"]["speed"] == 1.2
    assert payload["voice_settings"]["style"] == 0.4
    assert payload["model_id"] == "eleven_flash_v2_5"
    assert payload["language_code"] == "uk"


def test_tts_payload_per_model():
    v2 = tts_payload(TtsRequest(text="Так.", voice_id="v", model="eleven_multilingual_v2", previous_text="Ну."))
    assert "language_code" not in v2  # multilingual_v2 rejects it
    assert v2["previous_text"] == "Ну."
    v3 = tts_payload(TtsRequest(text="Так.", voice_id="v", model="eleven_v3", previous_text="Ну.", stability=0.3))
    assert "previous_text" not in v3
    assert v3["voice_settings"]["stability"] == 0.5  # v3: only 0 / 0.5 / 1
    assert v3["language_code"] == "uk"
    v4 = tts_payload(TtsRequest(text="Так.", voice_id="v", model="eleven_v4_turbo", previous_text="Ну.", stability=0.3))
    assert v4["model_id"] == "eleven_v4_turbo" and "previous_text" not in v4
    assert v4["voice_settings"]["stability"] == 0.3
    unknown = tts_payload(TtsRequest(text="Так.", voice_id="v", model="gpt-whatever"))
    assert unknown["model_id"] == "eleven_flash_v2_5"


def test_voice_tools_are_not_offered():
    names = {t["name"] for t in ELEVEN_TOOLS}
    assert "change_voice" not in names and "set_voice_style" not in names
    assert {"calendar_list_events", "gmail_search_messages", "web_search", "end_conversation"} <= names


def test_eleven_prompt_is_for_text_not_speech():
    prompt = build_eleven_prompt(
        language_name="українською", assistant_name=None, today="2026-10-09", feminine=False, tool_rules="RULES"
    )
    assert "Listening like a person" not in prompt  # no backchannels: a text model answers after the turn
    assert "Listening like a person" in LIVE_PROMPT  # Live keeps them
    assert "numbers" in prompt and "in words" in prompt
    assert "Tool rules:\nRULES" in prompt
    assert "я зрозумів" in prompt and "Today is 2026-10-09" in prompt


def test_history_items_for_realtime():
    log = ConversationLog()
    log.add("user", "Привіт")
    log.add("assistant", "Привіт! Що робимо?")
    log.add_note("сусіди говорили про ремонт")
    items = history_items(log.live_input())
    assert [i["role"] for i in items] == ["user", "assistant", "system"]
    assert items[1]["content"][0] == {"type": "output_text", "text": "Привіт! Що робимо?"}
    assert items[0]["content"][0]["type"] == "input_text"


def test_endpoints_need_the_key(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    monkeypatch.setattr(web.eleven, "api_key", "")
    client = TestClient(web.app)
    assert client.get("/api/eleven/voices", headers=HEADERS).status_code == 503
    assert client.post("/api/eleven/tts", headers=HEADERS, json={"text": "а", "voice_id": "v"}).status_code == 503
    assert client.get("/api/config").json()["eleven"] is False


def test_session_creates_a_text_only_realtime_call(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    monkeypatch.setattr(web.eleven, "api_key", "key")
    seen = {}

    async def create(*, sdp, session):
        seen["sdp"], seen["session"] = sdp, session
        headers = {"location": "/v1/realtime/calls/rtc_abc123"}
        return SimpleNamespace(text="answer-sdp", response=SimpleNamespace(headers=headers))

    async def no_run(self):
        return None

    monkeypatch.setattr(web.openai_client.realtime.calls, "create", create)
    monkeypatch.setattr(RealtimeToolBridge, "run", no_run)
    client = TestClient(web.app)
    res = client.post("/api/eleven/session", headers=HEADERS, json={"sdp": "offer", "feminine": True})
    assert res.status_code == 200
    assert res.json() == {"sdp": "answer-sdp", "session_id": "rtc_abc123"}
    session = seen["session"]
    assert session["output_modalities"] == ["text"]
    assert "я зрозуміла" in session["instructions"]
    assert all(t["name"] != "change_voice" for t in session["tools"])
    web.users.get(HEADERS["X-Client-Id"]).bridges.clear()


class _FakeConnection:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, event: dict) -> None:
        self.sent.append(event)


class _FakeExecutor:
    async def execute(self, name, arguments, ctx):
        return ToolResult(ok=True, status="ok", message=f"{name} done")


def test_bridge_runs_tool_calls_and_continues():
    async def scenario():
        log = ConversationLog()
        bridge = RealtimeToolBridge(
            client=None, call_id="rtc_1", executor=_FakeExecutor(), conversation=log, history=[]
        )
        conn = _FakeConnection()
        bridge._connection = conn
        await bridge._handle_event({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Що завтра?"})
        await bridge._handle_event({
            "type": "response.done",
            "response": {"output": [
                {"type": "function_call", "name": "calendar_list_events", "arguments": "{}", "call_id": "c1"},
                {"type": "function_call", "name": "end_conversation", "arguments": "{}", "call_id": "c2"},
            ]},
        })
        await asyncio.gather(*bridge._tool_tasks)
        return bridge, conn, log

    bridge, conn, log = asyncio.run(scenario())
    outputs = [e["item"]["call_id"] for e in conn.sent if e["type"] == "conversation.item.create"]
    assert outputs == ["c1", "c2"]
    assert conn.sent[-1]["type"] == "response.create"
    assert bridge.end_requested
    assert any("Що завтра?" in t.text for t in log.turns())
    assert any("calendar_list_events" in t.text for t in log.turns())


@pytest.mark.parametrize("event", [{"type": "response.done", "response": {"output": [{"type": "message"}]}}])
def test_bridge_ignores_answers_without_tools(event):
    async def scenario():
        bridge = RealtimeToolBridge(
            client=None, call_id="rtc_2", executor=_FakeExecutor(), conversation=ConversationLog(), history=[]
        )
        bridge._connection = _FakeConnection()
        await bridge._handle_event(event)
        return bridge

    assert not asyncio.run(scenario())._tool_tasks
