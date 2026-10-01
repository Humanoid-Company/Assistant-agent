"""OAuth / account manager scenarios with fakes — no real Google browser flow."""
from __future__ import annotations

from auth.account_manager import AccountManager
from auth.google_oauth import GoogleIdentity
from auth.token_store import InMemoryTokenStore
from tests.helpers_google import CALENDAR_ONLY_SCOPES, FakeOAuth, build_test_router


def test_oauth_success_connects_account(tmp_path):
    store = InMemoryTokenStore()
    identity = GoogleIdentity(sub="sub-1", email="one@example.com", name="One")
    oauth = FakeOAuth(store, {identity.sub: identity})
    mgr = AccountManager(oauth, store, tmp_path / "st.json")
    attempt = mgr.connect()
    assert attempt.ok
    st = attempt.status
    assert st.connected
    assert st.email == "one@example.com"
    # One consent screen covers everything — no later per-feature prompts.
    assert st.calendar_ready and st.gmail_ready and st.notes_ready
    assert "календар, пошта і нотатки доступні" in attempt.message
    assert oauth.authorize_calls == 1
    assert store.load("sub-1")


def test_minimal_connect_still_possible(tmp_path):
    store = InMemoryTokenStore()
    identity = GoogleIdentity(sub="sub-1", email="one@example.com", name="One")
    oauth = FakeOAuth(store, {identity.sub: identity})
    mgr = AccountManager(oauth, store, tmp_path / "st.json")
    st = mgr.connect(with_calendar=True, full_access=False).status
    assert st.calendar_ready
    assert not st.gmail_ready


def test_partial_consent_names_what_is_missing(tmp_path):
    """User unticked some boxes on Google's screen — say exactly what is missing and how to fix."""
    from auth.scopes import CALENDAR_SCOPES, IDENTITY_SCOPES

    store = InMemoryTokenStore()
    identity = GoogleIdentity(sub="sub-1", email="one@example.com", name="One")
    oauth = FakeOAuth(store, {identity.sub: identity})
    oauth._next_authorize_scopes = list(IDENTITY_SCOPES + CALENDAR_SCOPES)
    mgr = AccountManager(oauth, store, tmp_path / "st.json")
    attempt = mgr.connect()
    assert attempt.ok
    assert "Доступно: календар" in attempt.message
    assert "пошта, нотатки" in attempt.message

    second = mgr.request_full_access()  # one more consent adds everything missing
    assert second.status.gmail_ready and second.status.notes_ready and second.status.calendar_ready
    assert oauth.authorize_calls == 2


def test_status_does_not_wait_for_an_open_browser_consent(tmp_path):
    """While the user is on Google's screen, other tools must keep answering."""
    import threading

    store = InMemoryTokenStore()
    oauth = FakeOAuth(store)
    mgr = AccountManager(oauth, store, tmp_path / "st.json")
    in_browser = threading.Event()
    release = threading.Event()
    real_authorize = oauth.authorize

    def slow_authorize(scopes=None):
        in_browser.set()
        release.wait(5)
        return real_authorize(scopes)

    oauth.authorize = slow_authorize  # type: ignore[method-assign]
    worker = threading.Thread(target=mgr.connect)
    worker.start()
    assert in_browser.wait(2)
    done = threading.Event()
    threading.Thread(target=lambda: (mgr.status(), done.set())).start()
    assert done.wait(1), "status() blocked behind the browser consent"
    release.set()
    worker.join(5)


def test_oauth_denied_does_not_crash(tmp_path):
    store = InMemoryTokenStore()
    oauth = FakeOAuth(store)
    oauth.deny_next = True
    mgr = AccountManager(oauth, store, tmp_path / "st.json")
    attempt = mgr.connect()
    assert not attempt.ok
    assert not attempt.status.connected
    assert "відхил" in attempt.message.lower() or "скасован" in attempt.message.lower()


def test_oauth_cancel_keeps_alice_but_reports_failure(tmp_path):
    """Alice stays connected; cancelled Bob switch must not look like success."""
    store = InMemoryTokenStore()
    alice = GoogleIdentity(sub="sub-alice", email="alice@example.com", name="Alice")
    bob = GoogleIdentity(sub="sub-bob", email="bob@example.com", name="Bob")
    oauth = FakeOAuth(store, {alice.sub: alice})
    mgr = AccountManager(oauth, store, tmp_path / "st.json")
    first = mgr.connect(with_calendar=True)
    assert first.ok and first.status.email == "alice@example.com"

    oauth._identities[bob.sub] = bob
    oauth._next_identity = bob
    oauth.deny_next = True
    attempt = mgr.switch_via_reauth()
    assert attempt.ok is False
    assert attempt.status.connected is True
    assert attempt.status.email == "alice@example.com"
    assert attempt.status.last_auth_ok is False
    assert "перемикання не відбулось" in attempt.message.lower() or "скасован" in attempt.message.lower()

    # Router must not report success either.
    router, *_ = build_test_router(tmp_path)
    router.accounts._active_sub = "sub-alice"
    router.accounts._profiles["sub-alice"] = {"email": "alice@example.com", "name": "Alice"}
    oauth2 = router.accounts._oauth
    oauth2.deny_next = True
    result = router.reauth_switch()
    assert result.status != "success"
    assert result.data.get("auth_ok") is False
    assert result.data.get("still_connected") is True


def test_revoked_token_surfaces_reauth(tmp_path):
    router, cal, mail, oauth, accounts = build_test_router(tmp_path, scopes=CALENDAR_ONLY_SCOPES)
    oauth.revoked_subs.add("sub-alice")
    result = router.calendar_action(action="list")
    assert result.status == "auth_required"


def test_no_email_based_switch_method(tmp_path):
    router, *_ = build_test_router(tmp_path)
    assert not hasattr(router.accounts, "switch")


def test_reauth_switch_uses_browser_flow(tmp_path):
    router, cal, mail, oauth, accounts = build_test_router(tmp_path)
    before = oauth.authorize_calls
    attempt = accounts.switch_via_reauth()
    assert attempt.ok
    assert attempt.status.connected
    assert oauth.authorize_calls == before + 1
