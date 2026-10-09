"""The web page's Realtime engine: its call (OpenAI voice or ElevenLabs text), the brain behind
backend_task, the sideband that runs tools, and the ElevenLabs voice proxy."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import server.app as web
from prompts.live_prompt import LIVE_PROMPT, build_realtime_prompt
from server.eleven import TtsRequest, tts_payload
from server.realtime_engine import BRAIN_TOOLS, REALTIME_TOOLS, Brain, RealtimeBridge, history_text, realtime_session
from tools.executor import ToolExecutionContext
from tools.results import ToolResult
from voice.conversation import ConversationLog

HEADERS = {"X-Client-Id": "realtime-browser-0123456789"}


# ── ElevenLabs voice proxy ──


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


def test_eleven_endpoints_need_the_key(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    monkeypatch.setattr(web.eleven, "api_key", "")
    client = TestClient(web.app)
    assert client.get("/api/eleven/voices", headers=HEADERS).status_code == 503
    assert client.post("/api/eleven/tts", headers=HEADERS, json={"text": "а", "voice_id": "v"}).status_code == 503
    assert client.post(
        "/api/session", headers=HEADERS, json={"sdp": "o", "engine": "realtime", "tts": "eleven"}
    ).status_code == 503
    assert client.get("/api/config").json()["eleven"] is False


# ── tools, prompt, session ──


def test_tools_split_between_realtime_and_the_brain():
    direct = {t["name"] for t in REALTIME_TOOLS}
    brain = {t["name"] for t in BRAIN_TOOLS}
    assert direct == {"backend_task", "web_search", "check_connection", "set_assistant_name", "change_language", "end_conversation"}
    assert {"calendar_prepare_create", "gmail_prepare_send", "notes_add", "google_account"} <= brain
    assert not brain & {"web_search", "change_voice", "set_voice_style", "end_conversation"}


def test_realtime_prompt():
    common = dict(language_name="українською", assistant_name=None, today="2026-10-09")
    spoken = build_realtime_prompt(voice="cedar", history="User: Привіт\nYou: Привіт!", **common)
    assert "Listening like a person" not in spoken  # no backchannels: Realtime answers after the turn
    assert "Listening like a person" in LIVE_PROMPT
    assert "backend_task" in spoken and "you are talking, not reading" in spoken  # v2 by default
    assert "«Кедр»" in spoken and "я зрозумів" in spoken
    assert spoken.endswith("User: Привіт\nYou: Привіт!")
    assert "you are talking, not reading" not in build_realtime_prompt(voice="cedar", variant="v1", **common)
    text = build_realtime_prompt(text_output=True, feminine=True, **common)
    assert "in words" in text and "ElevenLabs" in text and "я зрозуміла" in text


def test_realtime_session_config():
    spoken = realtime_session(instructions="I", language="uk", voice="marin", speed="fast")
    assert spoken["output_modalities"] == ["audio"]
    assert spoken["audio"]["output"] == {"voice": "marin", "speed": 1.12}
    assert spoken["audio"]["input"]["transcription"]["language"] == "uk"
    text = realtime_session(instructions="I", language="uk", voice=None)
    assert text["output_modalities"] == ["text"] and "output" not in text["audio"]
    assert text["tools"] == REALTIME_TOOLS


def test_history_text():
    log = ConversationLog()
    log.add("user", "Привіт")
    log.add("assistant", "Привіт! Що робимо?")
    log.add_note("сусіди говорили про ремонт")
    text = history_text(log)
    assert text.startswith("User: Привіт\nYou: Привіт! Що робимо?\nNote: ")
    assert history_text(log, max_chars=10) == text[-10:]


def test_voices_per_engine(monkeypatch):
    client = TestClient(web.app)
    live = client.get("/api/voices").json()
    realtime = client.get("/api/voices?engine=realtime").json()
    assert "gleam" in {v["id"] for v in live["voices"]}
    ids = {v["id"] for v in realtime["voices"]}
    assert realtime["default"] == "marin" and "gleam" not in ids and {"marin", "cedar", "ash"} <= ids


def _fake_create(seen: dict):
    async def create(*, sdp, session):
        seen["sdp"], seen["session"] = sdp, session
        headers = {"location": "/v1/realtime/calls/rtc_abc123"}
        return SimpleNamespace(text="answer-sdp", response=SimpleNamespace(headers=headers))

    return create


def test_session_opens_a_realtime_call(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    seen: dict = {}

    async def no_run(self):
        return None

    async def live_create(**kwargs):
        raise AssertionError("a Realtime call must not open a Live session")

    monkeypatch.setattr(web.openai_client.realtime.calls, "create", _fake_create(seen))
    monkeypatch.setattr(web.openai_client.live, "create", live_create)
    monkeypatch.setattr(RealtimeBridge, "run", no_run)
    client = TestClient(web.app)
    user = web.users.get(HEADERS["X-Client-Id"])
    user.voice = "gleam"
    user.conversation.add("user", "Що в мене завтра?")
    body = {"sdp": "offer", "engine": "realtime", "voice": "ash", "speed": "slow"}
    res = client.post("/api/session", headers=HEADERS, json=body)
    assert res.status_code == 200
    assert res.json() == {"sdp": "answer-sdp", "session_id": "rtc_abc123"}
    session = seen["session"]
    assert session["audio"]["output"] == {"voice": "ash", "speed": 0.9}
    assert "«Еш»" in session["instructions"] and "Що в мене завтра?" in session["instructions"]
    assert user.voice == "gleam"  # Live's own voice choice is untouched
    assert [type(b).__name__ for b in user.bridges] == ["RealtimeBridge"]

    monkeypatch.setattr(web.eleven, "api_key", "key")
    res = client.post("/api/session", headers=HEADERS, json={"sdp": "o", "engine": "realtime", "tts": "eleven", "feminine": False})
    assert res.status_code == 200
    assert seen["session"]["output_modalities"] == ["text"]
    assert "я зрозумів" in seen["session"]["instructions"]
    user.bridges.clear()
    user.conversation.clear()


# ── the brain (backend_task) ──


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, name, arguments, ctx):
        self.calls.append(name)
        return ToolResult(ok=True, status="ok", message=f"{name} done")


class _FakeResponses:
    """First answer: a calendar call; second: the text to say. Records what it was asked."""

    def __init__(self, fail_with_previous: bool = False) -> None:
        self.requests: list[dict] = []
        self.fail_with_previous = fail_with_previous

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail_with_previous and kwargs.get("previous_response_id") == "stale":
            raise RuntimeError("previous response not found")
        if kwargs["input"][0].get("type") == "function_call_output":
            return SimpleNamespace(id="resp_2", output=[SimpleNamespace(type="message")], output_text="Завтра дві зустрічі.")
        call = SimpleNamespace(type="function_call", name="calendar_list_events", arguments="{}", call_id="c1")
        return SimpleNamespace(id="resp_1", output=[call], output_text="")


def _brain(responses: _FakeResponses, executor: _FakeExecutor, state: dict, log: ConversationLog | None = None) -> Brain:
    return Brain(
        client=SimpleNamespace(responses=responses), executor=executor, model="m", instructions="rules",
        state=state, conversation=log,
    )


def test_brain_runs_tools_and_keeps_its_thread():
    responses, executor, state, log = _FakeResponses(), _FakeExecutor(), {}, ConversationLog()
    brain = _brain(responses, executor, state, log)
    ctx = ToolExecutionContext(session_id="s", user_utterances=["Що завтра?"])
    answer = asyncio.run(brain.run("Що в календарі на завтра", ctx))
    assert answer == "Завтра дві зустрічі."
    assert executor.calls == ["calendar_list_events"]
    first, second = responses.requests
    assert "previous_response_id" not in first and "Що завтра?" in first["input"][0]["content"]
    assert second["previous_response_id"] == "resp_1"
    assert second["input"][0]["call_id"] == "c1"
    assert state["previous_id"] == "resp_2"  # the next task (a «так») continues this thread
    assert any("calendar_list_events" in t.text for t in log.turns())


def test_brain_starts_a_new_thread_when_the_old_one_is_gone():
    responses, executor = _FakeResponses(fail_with_previous=True), _FakeExecutor()
    state = {"previous_id": "stale"}
    answer = asyncio.run(_brain(responses, executor, state).run("Що завтра", ToolExecutionContext()))
    assert answer == "Завтра дві зустрічі."
    assert "previous_response_id" not in responses.requests[1]


# ── sideband ──


class _FakeConnection:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, event: dict) -> None:
        self.sent.append(event)


def test_bridge_runs_backend_task_and_direct_tools():
    async def scenario():
        log = ConversationLog()
        executor = _FakeExecutor()
        brain = _brain(_FakeResponses(), executor, {}, log)
        bridge = RealtimeBridge(client=None, call_id="rtc_1", executor=executor, brain=brain, conversation=log)
        conn = _FakeConnection()
        bridge._connection = conn
        await bridge._handle_event({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Що завтра?"})
        await bridge._handle_event({"type": "response.output_audio_transcript.done", "transcript": "Зараз гляну."})
        await bridge._handle_event({
            "type": "response.done",
            "response": {"output": [
                {"type": "function_call", "name": "backend_task", "arguments": json.dumps({"request": "Що завтра"}), "call_id": "c1"},
                {"type": "function_call", "name": "end_conversation", "arguments": "{}", "call_id": "c2"},
            ]},
        })
        await asyncio.gather(*bridge._tool_tasks)
        return bridge, conn, log, executor

    bridge, conn, log, executor = asyncio.run(scenario())
    items = [e["item"] for e in conn.sent if e["type"] == "conversation.item.create"]
    assert [i["call_id"] for i in items] == ["c1", "c2"]
    assert json.loads(items[0]["output"]) == {"ok": True, "message": "Завтра дві зустрічі."}
    assert conn.sent[-1]["type"] == "response.create"
    assert sorted(executor.calls) == ["calendar_list_events", "end_conversation"]  # they run side by side
    assert bridge.end_requested
    texts = [t.text for t in log.turns()]
    assert "Що завтра?" in texts and "Зараз гляну." in texts


def test_bridge_ignores_answers_without_tools():
    async def scenario():
        bridge = RealtimeBridge(
            client=None, call_id="rtc_2", executor=_FakeExecutor(), brain=None, conversation=ConversationLog()
        )
        bridge._connection = _FakeConnection()
        await bridge._handle_event({"type": "response.done", "response": {"output": [{"type": "message"}]}})
        return bridge

    assert not asyncio.run(scenario())._tool_tasks
