"""Regression: OAuth scopes must match actually granted permissions."""
from __future__ import annotations

import json

from auth.account_manager import AccountManager
from auth.google_oauth import GoogleIdentity
from auth.scopes import CALENDAR_SCOPES, GMAIL_READONLY_SCOPES, GMAIL_SCOPES, IDENTITY_SCOPES
from auth.token_store import InMemoryTokenStore
from google.oauth2.credentials import Credentials
from tests.helpers_google import CALENDAR_ONLY_SCOPES, FakeOAuth, _fake_creds, build_test_router


def test_calendar_only_gmail_reports_permission_required(tmp_path):
    router, _cal, mail, _oauth, _accounts = build_test_router(tmp_path, scopes=CALENDAR_ONLY_SCOPES)
    assert router.calendar_action(action="list").status == "success"
    r_mail = router.gmail_action(action="search", query="invoice")
    assert r_mail.status == "permission_required"
    assert len(mail.sent) == 0


def test_later_gmail_grant_keeps_calendar(tmp_path):
    router, _cal, mail, _oauth, accounts = build_test_router(tmp_path, scopes=CALENDAR_ONLY_SCOPES)
    assert router.calendar_action(action="list").status == "success"
    assert router.gmail_action(action="search", query="x").status == "permission_required"

    attempt = accounts.request_gmail_permission()
    assert attempt.ok
    st = attempt.status
    assert st.gmail_ready
    assert st.calendar_ready

    mail.messages["m1"] = {
        "id": "m1",
        "subject": "Hi",
        "from": "a@example.com",
        "snippet": "hello",
        "body_raw": "hello",
    }
    assert router.gmail_action(action="search", query="hi").status == "success"
    assert router.calendar_action(action="list").status == "success"


def test_restart_restores_actual_scopes_not_all_known(tmp_path):
    store = InMemoryTokenStore()
    store.save_record("sub-a", _fake_creds(scopes=CALENDAR_ONLY_SCOPES).to_json(), CALENDAR_ONLY_SCOPES)
    oauth = FakeOAuth(store, {"sub-a": GoogleIdentity("sub-a", "a@example.com", "A")})
    creds = oauth.load_credentials("sub-a")
    assert creds is not None
    assert oauth.has_scopes(creds, CALENDAR_SCOPES)
    assert not oauth.has_scopes(creds, GMAIL_SCOPES)


def test_gmail_readonly_only_survives_restart_no_send(tmp_path):
    """User requested all Gmail scopes but granted only readonly — after restart no send."""
    readonly = list(IDENTITY_SCOPES + CALENDAR_SCOPES + GMAIL_READONLY_SCOPES)
    store = InMemoryTokenStore()
    identity = GoogleIdentity(sub="sub-ro", email="ro@example.com", name="RO")
    # Simulate authorize that asked for GMAIL_SCOPES but token only has readonly.
    oauth = FakeOAuth(store, {identity.sub: identity})
    oauth._next_authorize_scopes = readonly
    mgr = AccountManager(oauth, store, tmp_path / "st.json")
    attempt = mgr.connect(with_calendar=True, with_gmail=True)
    assert attempt.ok
    assert attempt.status.gmail_readonly_ready
    assert not attempt.status.gmail_send_ready

    # "Restart": new AccountManager + OAuth on same store
    oauth2 = FakeOAuth(store, {identity.sub: identity})
    mgr2 = AccountManager(oauth2, store, tmp_path / "st.json")
    assert mgr2.status().gmail_readonly_ready
    assert not mgr2.status().gmail_send_ready
    assert not mgr2.status().gmail_ready

    router, _cal, mail, _oauth, accounts = build_test_router(tmp_path, scopes=readonly, seed_user=identity)
    r = router.gmail_action(action="send", to="x@example.com", subject="Hi", body="Body")
    assert r.status == "permission_required"
    assert len(mail.sent) == 0


def test_revoked_token_no_success(tmp_path):
    router, _cal, _mail, oauth, _accounts = build_test_router(tmp_path, scopes=CALENDAR_ONLY_SCOPES)
    oauth.revoked_subs.add("sub-alice")
    r = router.calendar_action(action="list")
    assert r.status == "auth_required"
    assert "готово" not in r.message.lower()
