"""Hosted web backend: per-browser isolation, access code, Google login over redirect."""
from __future__ import annotations

import asyncio
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


def test_voice_picker_lists_five_voices_and_saves_choice(client):
    data = client.get("/api/voices").json()
    ids = [v["id"] for v in data["voices"]]
    assert len(ids) == 5 and data["default"] in ids
    assert all(v["label"] and v["description"] for v in data["voices"])

    headers = {"X-Client-Id": ALICE}
    assert client.get("/api/me", headers=headers).json()["voice"] is None
    assert client.post("/api/voice", json={"voice": "shimmer"}, headers=headers).json() == {"voice": "shimmer"}
    assert client.get("/api/me", headers=headers).json()["voice"] == "shimmer"
    assert client.post("/api/voice", json={"voice": "nope"}, headers=headers).status_code == 400

    config = web._session_config(web.users.get(ALICE))
    assert config["audio"]["output"]["voice"] == "shimmer"
    assert "Your voice and character (Шиммер)" in config["instructions"]
    assert "feminine grammatical gender" in config["instructions"]


def test_voice_tool_accepts_only_picker_voices():
    user = web.users.get(BOB)
    ctx = ToolExecutionContext(session_id="s")
    bad = asyncio.run(user.executor.execute("change_voice", {"voice": "echo"}, ctx))
    assert not bad.ok and user.voice is None
    ok = asyncio.run(user.executor.execute("change_voice", {"voice": "coral"}, ctx))
    assert ok.ok and user.voice == "coral"
