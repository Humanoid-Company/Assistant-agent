"""Gmail draft fingerprint + shared-device session idle lock."""
from __future__ import annotations

import time

from auth.account_manager import AccountManager
from auth.google_oauth import GoogleIdentity
from auth.token_store import InMemoryTokenStore
from tests.helpers_google import FakeOAuth, build_test_router


def test_draft_changed_blocks_send_until_new_confirm(tmp_path):
    router, _cal, mail, *_ = build_test_router(tmp_path)
    d = router.gmail_action(
        action="draft",
        to="boss@example.com",
        subject="Звіт",
        body="Версія 1",
    )
    assert d.status == "success"
    draft_id = d.data["draft_id"]

    propose = router.gmail_action(action="send", draft_id=draft_id)
    assert propose.status == "confirmation_required"

    # Draft mutated externally between propose and confirm.
    mail.drafts[draft_id] = {
        "to": "other@example.com",
        "subject": "Інша тема",
        "body": "Версія 2 — змінено",
    }
    blocked = router.gmail_action(action="confirm", confirmation="yes")
    assert blocked.status == "needs_more_info"
    assert "змінил" in blocked.message.lower()
    assert mail.sent == []

    # New propose + confirm with updated content succeeds once.
    again = router.gmail_action(action="send", draft_id=draft_id)
    assert again.status == "confirmation_required"
    ok = router.gmail_action(action="confirm", confirmation="yes")
    assert ok.status == "success"
    assert len(mail.sent) == 1
    assert mail.sent[0]["to"] == "other@example.com"


def test_shared_device_idle_clears_active_sub(tmp_path):
    store = InMemoryTokenStore()
    identity = GoogleIdentity(sub="sub-alice", email="alice@example.com", name="Alice")
    oauth = FakeOAuth(store, {identity.sub: identity})
    mgr = AccountManager(
        oauth,
        store,
        tmp_path / "st.json",
        shared_device=True,
        session_idle_timeout_s=0.05,
    )
    attempt = mgr.connect(with_calendar=True)
    assert attempt.ok
    assert mgr.active_sub() == "sub-alice"

    time.sleep(0.08)
    assert mgr.active_sub() is None
    st = mgr.status()
    assert not st.connected
    assert st.session_locked


def test_shared_device_restart_does_not_auto_resume(tmp_path):
    store = InMemoryTokenStore()
    identity = GoogleIdentity(sub="sub-alice", email="alice@example.com", name="Alice")
    oauth = FakeOAuth(store, {identity.sub: identity})
    mgr = AccountManager(oauth, store, tmp_path / "st.json", shared_device=False)
    assert mgr.connect(with_calendar=True).ok

    # Restart in shared mode — must not reopen Alice's mailbox automatically.
    oauth2 = FakeOAuth(store, {identity.sub: identity})
    mgr2 = AccountManager(oauth2, store, tmp_path / "st.json", shared_device=True)
    assert mgr2.active_sub() is None
    assert not mgr2.status().connected


def test_personal_mode_keeps_session(tmp_path):
    router, _cal, _mail, _oauth, accounts = build_test_router(
        tmp_path, shared_device=False, session_idle_timeout_s=0.01
    )
    time.sleep(0.05)
    assert accounts.active_sub() == "sub-alice"


def test_lock_session_tool(tmp_path):
    router, *_ = build_test_router(tmp_path, shared_device=True, session_idle_timeout_s=9999)
    assert router.accounts.active_sub() == "sub-alice"
    r = router.lock_session()
    assert r.status == "success"
    assert router.accounts.active_sub() is None
