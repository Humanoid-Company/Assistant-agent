"""Hosted web backend: per-browser isolation, access code, Google login over redirect."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import server.app as web
from auth.google_oauth import GoogleIdentity
from auth.web_oauth import WebOAuth
from tests.helpers_google import FULL_SCOPES, _fake_creds
from tools.executor import ToolExecutionContext

ALICE = "alice-browser-0123456789"
BOB = "bob-browser-0123456789ab"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    return TestClient(web.app)


def test_health_and_config(client):
    assert client.get("/healthz").json()["ok"] is True
    assert set(client.get("/api/config").json()) == {"access_code_required", "google_login"}


def test_access_code_is_enforced(client, monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "team-secret")
    assert client.get("/api/me", headers={"X-Client-Id": ALICE}).status_code == 401
    ok = client.get("/api/me", headers={"X-Client-Id": ALICE, "X-Access-Code": "team-secret"})
    assert ok.status_code == 200


def test_client_id_is_validated(client):
    assert client.get("/api/me", headers={"X-Client-Id": "short"}).status_code == 400


def test_each_browser_has_its_own_google_account(client):
    alice = web.users.get(ALICE)
    alice.router.accounts.activate(
        GoogleIdentity("sub-alice", "alice@example.com", "Alice"),
        credentials_json=_fake_creds(scopes=FULL_SCOPES).to_json(),
        granted=FULL_SCOPES,
    )
    assert client.get("/api/me", headers={"X-Client-Id": ALICE}).json()["google"]["email"] == "alice@example.com"
    assert client.get("/api/me", headers={"X-Client-Id": BOB}).json()["google"]["connected"] is False


def test_voice_connect_request_points_to_the_page_button():
    user = web.users.get(BOB)
    result = asyncio.run(
        user.executor.execute("google_account", {"action": "connect"}, ToolExecutionContext(session_id="s"))
    )
    assert "Підключити Google" in result.message


def test_google_login_asks_for_everything_and_remembers_who_started_it():
    oauth = WebOAuth(client_id="cid", client_secret="secret", redirect_uri="https://api.example/auth/google/callback")
    url = oauth.authorization_url(ALICE)
    query = parse_qs(urlparse(url).query)
    scopes = query["scope"][0]
    for needed in ("calendar", "gmail.readonly", "gmail.send", "gmail.compose", "drive.file"):
        assert needed in scopes
    assert query["access_type"] == ["offline"]
    assert query["redirect_uri"] == ["https://api.example/auth/google/callback"]
    assert oauth._pending[query["state"][0]].client_id == ALICE
    with pytest.raises(ValueError):
        oauth.finish(state="forged", code="x")


def test_cancelled_google_login_page(client):
    page = client.get("/auth/google/callback?error=access_denied")
    assert page.status_code == 400
    assert "google-login" in page.text


class _FakeSideband:
    """Stands in for client.live.sideband.connect(): stays open until session.close()."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.close_calls = 0
        outer = self

        class _Session:
            async def close(self) -> None:
                outer.close_calls += 1
                outer.closed.set()

        self.session = _Session()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.closed.wait()
        raise StopAsyncIteration


class _FakeLiveClient:
    def __init__(self) -> None:
        self.connections: list[_FakeSideband] = []
        outer = self

        class _Sideband:
            def connect(self, *, session_id: str) -> _FakeSideband:
                conn = _FakeSideband()
                outer.connections.append(conn)
                return conn

        class _Live:
            sideband = _Sideband()

        self.live = _Live()


def test_forgotten_call_is_closed_after_the_time_limit():
    from server.live_bridge import SidebandToolBridge

    fake = _FakeLiveClient()
    closed: list[object] = []
    bridge = SidebandToolBridge(
        client=fake,  # type: ignore[arg-type]
        session_id="sess-1",
        executor=None,  # type: ignore[arg-type]
        on_closed=closed.append,
        max_duration_s=0.05,
    )
    asyncio.run(asyncio.wait_for(bridge.run(), timeout=2))
    assert fake.connections[0].close_calls == 1
    assert closed == [bridge]
    assert not bridge.is_open


def test_new_call_closes_the_previous_one_of_the_same_browser(client, monkeypatch):
    class _Old:
        closed = False

        async def close(self) -> None:
            _Old.closed = True

    class _Created:
        class session:  # noqa: N801
            id = "sess-new"

        class transport:  # noqa: N801
            sdp = "answer"

    async def fake_create(**_kwargs):
        return _Created

    started: list[object] = []
    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0, started.append(self)))
    user = web.users.get(ALICE)
    old = _Old()
    user.bridges.add(old)

    res = client.post("/api/session", json={"sdp": "offer"}, headers={"X-Client-Id": ALICE})
    assert res.status_code == 200
    assert res.json()["sdp"] == "answer"
    assert _Old.closed
    user.bridges.discard(old)
    assert [b.session_id for b in user.bridges] == ["sess-new"]
    user.bridges.clear()


def test_keeps_itself_awake_only_when_deployed(monkeypatch):
    import time

    import httpx

    pinged: list[str] = []

    async def fake_get(self, url, **_kwargs):
        pinged.append(url)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(web, "KEEP_AWAKE_S", 0.05)

    monkeypatch.setattr(web, "PUBLIC_BACKEND_URL", "http://localhost:8000")
    with TestClient(web.app):
        time.sleep(0.2)
    assert pinged == []

    monkeypatch.setattr(web, "PUBLIC_BACKEND_URL", "https://backend.example")
    with TestClient(web.app):
        time.sleep(0.3)
    assert pinged and set(pinged) == {"https://backend.example/healthz"}


def test_voice_picker_lists_live_voices_and_saves_choice(client):
    data = client.get("/api/voices").json()
    ids = [v["id"] for v in data["voices"]]
    assert {"marin", "cedar", "gleam", "willow", "meridian"} <= set(ids) and data["default"] in ids
    assert any(v["feminine"] for v in data["voices"]) and not all(v["feminine"] for v in data["voices"])
    assert all(v["label"] and v["description"] for v in data["voices"])

    headers = {"X-Client-Id": ALICE}
    assert client.get("/api/me", headers=headers).json()["voice"] is None
    assert client.post("/api/voice", json={"voice": "willow"}, headers=headers).json()["voice"] == "willow"
    assert client.get("/api/me", headers=headers).json()["voice"] == "willow"
    assert client.post("/api/voice", json={"voice": "nope"}, headers=headers).status_code == 400

    config = web._session_config(web.users.get(ALICE))
    assert config["audio"]["output"]["voice"] == "willow"
    assert "voice preset «Віллоу»" in config["instructions"]
    assert "feminine grammatical gender" in config["instructions"]


def test_voice_tool_accepts_only_picker_voices():
    user = web.users.get(BOB)
    ctx = ToolExecutionContext(session_id="s")
    bad = asyncio.run(user.executor.execute("change_voice", {"voice": "echo"}, ctx))
    assert not bad.ok and user.voice is None
    ok = asyncio.run(user.executor.execute("change_voice", {"voice": "gleam"}, ctx))
    assert ok.ok and user.voice == "gleam"


def test_voice_change_keeps_the_conversation(client):
    """Conversation → voice change by voice → the new session starts with everything said before."""
    from server.live_bridge import SidebandToolBridge

    user = web.users.get("carol-browser-0123456789")
    bridge = SidebandToolBridge(
        client=None, session_id="s1", executor=user.executor, conversation=user.conversation
    )
    user.bridges.add(bridge)

    async def talk():
        for etype, delta in [
            ("session.input_transcript.delta", "Мене звати Остап, "),
            ("session.input_transcript.delta", "я п'ю каву без цукру."),
            ("session.output_transcript.delta", "Приємно, Остапе! Запам'ятала."),
            ("session.input_transcript.delta", "Зміни голос на чоловічий."),
        ]:
            await bridge._handle_event({"type": etype, "delta": delta})

    asyncio.run(talk())
    result = asyncio.run(
        user.executor.execute("change_voice", {"voice": "meridian"}, ToolExecutionContext(session_id="s1"))
    )
    assert result.ok and user.voice == "meridian"
    assert bridge.restart_for_voice  # the page reconnects with the new voice…
    assert client.get("/api/me", headers={"X-Client-Id": "carol-browser-0123456789"}).json()["reconnect"] is True

    config = web._session_config(user)  # …and the new session gets the whole conversation
    assert config["audio"]["output"]["voice"] == "meridian"
    texts = [(item["role"], item["content"][0]["text"]) for item in config["input"]]
    assert texts == [
        ("user", "Мене звати Остап, я п'ю каву без цукру."),
        ("assistant", "Приємно, Остапе! Запам'ятала."),
        ("user", "Зміни голос на чоловічий."),
    ]
    assert "you are always Єва" in config["instructions"]


def test_speed_and_style_are_saved_and_returned_for_the_page(client):
    """The page sends the returned instruction itself once Єва is quiet; the server only stores."""
    user = web.users.get("dave-browser-0123456789a")
    sent = []

    class FakeBridge:
        async def append_instruction(self, text):
            sent.append(text)

    user.bridges.add(FakeBridge())
    headers = {"X-Client-Id": "dave-browser-0123456789a"}
    body = client.post("/api/voice", json={"speed": "slow", "style": "calm"}, headers=headers).json()
    assert body["speed"] == "slow" and body["style"] == "calm"
    assert "slower" in body["instruction"] and "calm" in body["instruction"] and "not a message" in body["instruction"]
    assert sent == []  # not pushed mid-answer by the server
    assert "slower" in web._session_config(user)["instructions"]  # a new session starts with them
    assert client.post("/api/voice", json={"speed": "slow"}, headers=headers).json()["instruction"] == ""
    assert client.post("/api/voice", json={"speed": "warp"}, headers=headers).status_code == 400
    user.bridges.clear()


def test_new_conversation_and_sign_out_clear_history(client):
    headers = {"X-Client-Id": "erin-browser-0123456789a"}
    user = web.users.get("erin-browser-0123456789a")
    user.conversation.add("user", "секрет")
    assert client.delete("/api/conversation", headers=headers).json()["ok"]
    assert len(user.conversation) == 0 and "input" not in web._session_config(user)
    user.conversation.add("user", "секрет")
    client.post("/api/google/disconnect", headers=headers)
    assert len(user.conversation) == 0


def test_voice_change_closes_the_call_without_a_spoken_confirmation():
    """change_voice: no «перемикаю…» — the old session is closed, the new voice carries on."""
    from server.live_bridge import SidebandToolBridge

    user = web.users.get("frank-browser-0123456789")
    sent = []

    class Conn:
        class session:
            @staticmethod
            async def close():
                sent.append("close")

        class response:
            @staticmethod
            async def create(**kw):
                sent.append("response.create")

            class item:
                @staticmethod
                async def create(**kw):
                    sent.append("item.create")

    bridge = SidebandToolBridge(client=None, session_id="s2", executor=user.executor)
    bridge._connection = Conn()
    user.bridges.add(bridge)
    asyncio.run(
        bridge._execute_and_continue(
            name="change_voice", arguments={"voice": "stone"}, call_id="c1", delegation_id=None, key=""
        )
    )
    assert user.voice == "stone" and user.reconnect_pending
    assert sent == ["close"]  # no tool output / response.create → she says nothing in the old voice
    user.bridges.clear()


def test_voice_request_detection_for_the_nudge():
    from voice.options import VOICE_REQUEST_RE as voice_request

    for text in ["Єва, зміни голос на чоловічий", "постав інший голос", "поміняй, будь ласка, голос",
                 "давай голос на жіночий", "Голос на спокійніший"]:
        assert voice_request.search(text), text
    for text in ["голос у тебе гарний", "зміни зустріч на завтра", "зроби нагадування про голосування"]:
        assert not voice_request.search(text), text


def test_voice_request_picks_the_voice():
    from voice.options import voice_request_target as target

    assert target("Єва, зміни голос на чоловічий", "gleam") == "meridian"
    assert target("зміни голос на чоловічий", "meridian") == "ripple"  # already a man: the next one
    assert target("постав жіночий голос", "stone") == "gleam"
    assert target("зміни голос на інший", "gleam") == "bossa"
    assert target("зроби голос спокійніший", "gleam") == "willow"
    assert target("зроби голос спокійніший", "tempo") == "stone"
    assert target("постав голос Босу", "gleam") == "bossa"
    assert target("переключи голос на Кедра", "gleam") == "cedar"
    assert target("що в мене завтра?", "gleam") is None
    assert target("Єва, змини голос на чоловічий", "gleam") == "meridian"  # STT spelling
    assert target("Єва, зміни голос", "gleam") == "bossa"  # bare: just another one
    assert target("Єва, змини голос на Нагадай, як мене звати", "gleam") is None  # cut off: unclear


def test_bridge_switches_voice_from_the_transcript(monkeypatch):
    """The model said only «Секунду»: the server switches the voice from what the user said."""
    import server.live_bridge as bridge_mod
    from server.live_bridge import SidebandToolBridge

    monkeypatch.setattr(bridge_mod, "_UTTERANCE_PAUSE_S", 0.01)
    monkeypatch.setattr(bridge_mod, "_PAGE_FIRST_S", 0)
    user = web.users.get("gina-browser-0123456789a")
    user.voice = "gleam"
    closed = []

    class Conn:
        class session:
            @staticmethod
            async def close():
                closed.append(True)

    bridge = SidebandToolBridge(
        client=None, session_id="s3", executor=user.executor, on_voice_request=user.switch_voice_by_request
    )
    bridge._connection = Conn()

    async def talk():
        for delta in ["Єва, зміни голос на ", "чоловічий."]:
            await bridge._handle_event({"type": "session.input_transcript.delta", "delta": delta})
        await asyncio.sleep(0.1)

    asyncio.run(talk())
    assert user.voice == "meridian" and user.reconnect_pending and closed == [True]


def test_voice_request_endpoint_and_dedup(client):
    headers = {"X-Client-Id": "hank-browser-0123456789a"}
    user = web.users.get("hank-browser-0123456789a")
    user.voice = "gleam"
    first = client.post("/api/voice-request", json={"text": "Єва, зміни голос на чоловічий"}, headers=headers).json()
    assert first == {"switched": True, "voice": "meridian"}
    # the same request arriving again (Live transcript / the model): no second switch
    again = client.post("/api/voice-request", json={"text": "Єва, зміни голос на чоловічий"}, headers=headers).json()
    assert again == {"switched": False, "voice": "meridian"}
    result = asyncio.run(
        user.executor.execute("change_voice", {"voice": "ripple"}, ToolExecutionContext(session_id="s"))
    )
    assert result.ok and user.voice == "meridian"
    other = client.post("/api/voice-request", json={"text": "котра година?"}, headers=headers).json()
    assert other["switched"] is False


def test_speed_change_waits_until_she_finishes_speaking():
    """An instruction appended mid-answer derails it: it is sent once she is quiet, latest only."""
    from server.live_bridge import SidebandToolBridge

    sent: list[str] = []

    class Conn:
        class session:
            class instructions:
                @staticmethod
                async def append(content, **kw):
                    sent.append(content)

    async def scenario():
        bridge = SidebandToolBridge(client=None, session_id="s4", executor=None)
        bridge._connection = Conn()
        for word in ["Жила-була ", "маленька "]:
            await bridge._handle_event({"type": "session.output_transcript.delta", "delta": word})
        await bridge.append_instruction("slow", quiet_s=0.3)
        await asyncio.sleep(0.1)
        await bridge.append_instruction("calm", quiet_s=0.3)  # replaces «slow»
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": "дівчинка."})
        await asyncio.sleep(0.15)
        assert sent == []  # still talking
        await asyncio.sleep(0.5)
        assert sent == ["calm"]

    asyncio.run(scenario())


def test_model_cannot_change_the_voice_nobody_asked_for():
    """Prod: «Розкажи щось» → the model called change_voice on its own and the voice switched."""
    user = web.users.get("voice-guard-0123456789")
    user.voice, user.voice_switched_at = "gleam", 0.0
    unasked = ToolExecutionContext(session_id="s", user_utterances=["Як твої справи", "Розкажи щось"])
    refused = asyncio.run(user.executor.execute("change_voice", {"voice": "bossa"}, unasked))
    assert not refused.ok and user.voice == "gleam" and not user.reconnect_pending
    asked = ToolExecutionContext(session_id="s", user_utterances=["Розкажи щось", "Зроби чоловічий голос"])
    done = asyncio.run(user.executor.execute("change_voice", {"voice": "meridian"}, asked))
    assert done.ok and user.voice == "meridian" and user.reconnect_pending
    assert "попросила модель" in user.voice_switch_note


@pytest.mark.parametrize(
    "utterances, expected",
    [
        (None, True),
        (["Розкажи щось"], False),
        (["Як справи", "Розкажи щось цікаве"], False),
        (["Давай іншим голосом"], True),
        (["Хочу Босу"], True),
        (["зроби чоловічий"], True),
        (["говори спокійніше"], True),
    ],
)
def test_asked_for_voice_change(utterances, expected):
    from voice.options import asked_for_voice_change

    assert asked_for_voice_change(utterances) is expected



def test_wake_request_goes_into_the_new_call_as_the_users_message(monkeypatch):
    """Prod: «Єва, скажи, яка в мене завтра подія» → she said «Яка в мене завтра подія?». Quoted in a
    commentary GPT-Live sometimes reads it back (1 of 3); as the user's message in the history, 0 of 3."""
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    captured = {}

    async def fake_create(*, session, transport):
        captured["input"] = session.get("input", [])
        return SimpleNamespace(session=SimpleNamespace(id="sess-wake"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    user = web.users.get("wake-req-0123456789ab")
    user.conversation.clear()
    res = TestClient(web.app).post(
        "/api/session",
        headers={"X-Client-Id": "wake-req-0123456789ab"},
        json={"sdp": "offer", "user_text": "Єва, скажи, яка в мене завтра подія"},
    )
    assert res.status_code == 200
    last = captured["input"][-1]
    assert last["role"] == "user" and "яка в мене завтра подія" in last["content"][0]["text"]


def test_voice_switch_keeps_the_old_call_until_the_page_swaps(monkeypatch):
    """keep_old: the new call in another voice opens beside the talking one; the page closes the old
    call when it swaps — and the server does it anyway if both are still open much later."""
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    monkeypatch.setattr(web, "KEEP_OLD_MAX_S", 0.05)

    class _Old:
        session_id = "sess-old"
        is_open = True
        closed = False

        async def close(self) -> None:
            _Old.closed = True

    async def fake_create(**_kwargs):
        return SimpleNamespace(session=SimpleNamespace(id="sess-standby"), transport=SimpleNamespace(sdp="answer"))

    async def fake_run(self):
        self._connection = object()  # stays open
        await asyncio.sleep(1)

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", fake_run)
    client_id = "keep-old-0123456789ab"
    user = web.users.get(client_id)
    user.bridges.clear()
    old = _Old()
    user.bridges.add(old)
    with TestClient(web.app) as client:
        res = client.post("/api/session", json={"sdp": "offer", "keep_old": True}, headers={"X-Client-Id": client_id})
        assert res.status_code == 200
        assert not _Old.closed  # still talking while the new call comes up
        import time

        time.sleep(0.3)
    assert _Old.closed  # the page never swapped: closed by the server
    user.bridges.clear()


def test_prompt_variant_v2_is_used_only_when_the_page_asks(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    sent: list[str] = []

    async def fake_create(*, session, transport):
        sent.append(session["instructions"])
        return SimpleNamespace(session=SimpleNamespace(id=f"sess-p{len(sent)}"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    headers = {"X-Client-Id": "prompt-var-0123456789ab"}
    client = TestClient(web.app)
    for body in ({"sdp": "o", "prompt": "v2"}, {"sdp": "o"}, {"sdp": "o", "prompt": "nonsense"}):
        assert client.post("/api/session", headers=headers, json=body).status_code == 200
    v2, v1, fallback = sent
    assert "you are talking, not reading" in v2 and "audiobook" in v2
    assert "you are talking, not reading" not in v1 and v1 == fallback
    for text in (v1, v2):  # the shared parts stay in both
        assert "Voice changes:" in text and "Interruption policy" in text
    web.users.get(headers["X-Client-Id"]).bridges.clear()


def test_never_russian():
    """A team decision: no Russian, not even when the user asks for it."""
    from prompts.live_prompt import build_live_prompt
    from tools.live_schemas import LIVE_BACKEND_TOOLS
    from voice.options import LANGUAGE_OPTIONS

    assert "ru" not in LANGUAGE_OPTIONS
    tool = next(t for t in LIVE_BACKEND_TOOLS if t.get("name") == "change_language")
    assert "ru" not in str(tool)
    prompt = build_live_prompt(language_name="українською", assistant_name=None, today="2026-10-08")
    assert "Never speak Russian" in prompt
