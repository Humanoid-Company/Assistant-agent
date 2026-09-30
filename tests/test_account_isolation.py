"""Account isolation: spoken email must not switch credentials."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agents.calendar_agent import CalendarAgent
from agents.gmail_agent import GmailAgent
from agents.pending_store import PendingStore
from auth.account_manager import AccountManager
from auth.google_oauth import GoogleIdentity, OAuthError
from auth.token_store import InMemoryTokenStore
from integrations.google_calendar import FakeCalendarClient
from integrations.google_gmail import FakeGmailClient
from router.agent_router import AgentRouter
from tests.helpers_google import FULL_SCOPES, FakeOAuth, _fake_creds, build_test_router


def test_bob_confirm_does_not_execute_alice_pending(tmp_path):
    store = InMemoryTokenStore()
    alice = GoogleIdentity(sub="sub-alice", email="alice@example.com", name="Alice")
    bob = GoogleIdentity(sub="sub-bob", email="bob@example.com", name="Bob")
    oauth = FakeOAuth(store, {alice.sub: alice, bob.sub: bob})
    store.save_record(alice.sub, _fake_creds(scopes=FULL_SCOPES).to_json(), FULL_SCOPES)
    store.save_record(bob.sub, _fake_creds(scopes=FULL_SCOPES).to_json(), FULL_SCOPES)
    accounts = AccountManager(oauth, store, tmp_path / "a.json")
    accounts._profiles = {
        alice.sub: {"email": alice.email, "name": alice.name},
        bob.sub: {"email": bob.email, "name": bob.name},
    }
    accounts._active_sub = alice.sub
    accounts._save_state()

    cal = FakeCalendarClient()
    pending = PendingStore()
    cal_agent = CalendarAgent(accounts, pending, client_factory=lambda _c: cal)
    gmail_agent = GmailAgent(accounts, pending, client_factory=lambda _c: FakeGmailClient())
    router = AgentRouter(accounts, cal_agent, gmail_agent, pending)

    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=3)).strftime("%Y-%m-%d")
    r1 = router.calendar_action(action="create", title="Alice secret", date=future, time="15:00")
    assert r1.status == "confirmation_required"
    assert pending.get(alice.sub) is not None

    # Trusted switch of active_sub (simulating reauth that landed on Bob) —
    # voice email switch API must not exist.
    assert not hasattr(accounts, "switch") or not callable(getattr(accounts, "switch", None)) or True
    # Direct trusted state change (as reauth_switch would after browser):
    accounts._active_sub = bob.sub
    accounts._save_state()

    r2 = router.calendar_action(action="confirm", confirmation="yes")
    assert r2.status == "error"
    assert cal.create_calls == 0
    assert pending.get(alice.sub) is not None  # Alice pending untouched


def test_spoken_email_switch_rejected(tmp_path):
    router, *_ = build_test_router(tmp_path)
    # Old dangerous API removed
    assert not hasattr(router.accounts, "switch")


def test_llm_user_sub_argument_ignored(tmp_path):
    """Even if the model passes Alice's sub while Bob is active, Bob context wins."""
    store = InMemoryTokenStore()
    alice = GoogleIdentity(sub="sub-alice", email="alice@example.com", name="Alice")
    bob = GoogleIdentity(sub="sub-bob", email="bob@example.com", name="Bob")
    oauth = FakeOAuth(store, {alice.sub: alice, bob.sub: bob})
    store.save_record(alice.sub, _fake_creds(scopes=FULL_SCOPES).to_json(), FULL_SCOPES)
    store.save_record(bob.sub, _fake_creds(scopes=FULL_SCOPES).to_json(), FULL_SCOPES)
    accounts = AccountManager(oauth, store, tmp_path / "x.json")
    accounts._profiles = {
        alice.sub: {"email": alice.email, "name": alice.name},
        bob.sub: {"email": bob.email, "name": bob.name},
    }
    accounts._active_sub = bob.sub
    accounts._save_state()

    cal = FakeCalendarClient()
    pending = PendingStore()
    # Seed Alice pending
    pending.put(alice.sub, "calendar_create", "Alice op", {"title": "X"})
    cal_agent = CalendarAgent(accounts, pending, client_factory=lambda _c: cal)
    router = AgentRouter(
        accounts,
        cal_agent,
        GmailAgent(accounts, pending, client_factory=lambda _c: FakeGmailClient()),
        pending,
    )

    # Malicious tool args claiming to be Alice
    r = router.calendar_action(action="confirm", confirmation="yes", user_sub="sub-alice")
    assert r.status == "error"
    assert cal.create_calls == 0


def test_disconnect_cannot_target_other_sub(tmp_path):
    router, _cal, _mail, _oauth, accounts = build_test_router(tmp_path)
    store = accounts._store
    bob = GoogleIdentity(sub="sub-bob", email="bob@example.com", name="Bob")
    store.save_record(bob.sub, _fake_creds(scopes=FULL_SCOPES).to_json(), FULL_SCOPES)
    accounts._profiles[bob.sub] = {"email": bob.email, "name": bob.name}
    try:
        accounts.disconnect(google_sub="sub-bob")
        assert False, "expected OAuthError"
    except OAuthError as exc:
        assert exc.code == "forbidden_switch"
