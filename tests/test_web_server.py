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
    assert client.get("/healthz").json() == {"ok": True}
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
    robot = asyncio.run(
        user.executor.execute("control_robot", {"action": "sit"}, ToolExecutionContext(session_id="s"))
    )
    assert robot.ok is False


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
