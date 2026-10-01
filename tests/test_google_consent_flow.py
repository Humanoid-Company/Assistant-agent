"""Google sign-in runs in the background: one consent for all permissions, the conversation
keeps going while the user is in the browser, and the outcome is reported when it ends."""
from __future__ import annotations

import threading

from auth.google_oauth import GoogleIdentity
from tests.helpers_google import CALENDAR_ONLY_SCOPES, build_test_router


def _wait(results: list, done: threading.Event):
    assert done.wait(5), "consent outcome was never reported"
    return results[0]


def _collector():
    results: list = []
    done = threading.Event()

    def on_done(result):
        results.append(result)
        done.set()

    return results, done, on_done


def test_connect_returns_immediately_and_reports_full_access_later(tmp_path):
    router, _cal, _mail, oauth, accounts = build_test_router(tmp_path)
    accounts.disconnect()
    release = threading.Event()
    real_authorize = oauth.authorize

    def user_in_browser(scopes=None):
        release.wait(5)  # the user is still on Google's page
        return real_authorize(scopes)

    oauth.authorize = user_in_browser  # type: ignore[method-assign]
    results, done, on_done = _collector()

    opened = router.start_consent("connect", on_done)

    assert opened.data["consent_pending"] is True
    assert "всі галочки" in opened.message
    assert not done.is_set()
    # Meanwhile the assistant can still answer account questions.
    assert router.google_status().status in ("auth_required", "success")

    release.set()
    final = _wait(results, done)
    assert final.status == "success"
    assert final.data["calendar_ready"] and final.data["gmail_ready"] and final.data["notes_ready"]
    assert "календар, пошта і нотатки доступні" in final.message


def test_second_consent_while_one_is_open_is_refused(tmp_path):
    router, _cal, _mail, oauth, _accounts = build_test_router(tmp_path)
    release = threading.Event()
    real_authorize = oauth.authorize
    oauth.authorize = lambda scopes=None: (release.wait(5), real_authorize(scopes))[1]  # type: ignore[method-assign]
    results, done, on_done = _collector()

    router.start_consent("connect", on_done)
    again = router.start_consent("reauth_switch", lambda r: None)
    assert again.data.get("consent_pending") is True
    assert "уже відкрите" in again.message

    release.set()
    _wait(results, done)
    # Finished → a new consent may start.
    results2, done2, on_done2 = _collector()
    assert "всі галочки" in router.start_consent("grant_all", on_done2).message
    _wait(results2, done2)


def test_grant_all_adds_every_missing_permission_in_one_go(tmp_path):
    router, _cal, _mail, oauth, _accounts = build_test_router(tmp_path, scopes=CALENDAR_ONLY_SCOPES)
    assert router.gmail_action(action="search", query="x").status == "permission_required"
    calls_before = oauth.authorize_calls
    results, done, on_done = _collector()

    router.start_consent("grant_gmail", on_done)  # legacy name → same full grant
    final = _wait(results, done)

    assert final.status == "success"
    assert final.data["gmail_ready"] and final.data["notes_ready"] and final.data["calendar_ready"]
    assert oauth.authorize_calls == calls_before + 1


def test_cancelled_consent_is_reported_not_silent(tmp_path):
    router, _cal, _mail, oauth, _accounts = build_test_router(tmp_path)
    oauth.deny_next = True
    results, done, on_done = _collector()
    router.start_consent("reauth_switch", on_done)
    final = _wait(results, done)
    assert final.status in ("permission_denied", "auth_required")
    assert final.message


def test_switch_to_new_user_gets_full_access(tmp_path):
    router, _cal, _mail, oauth, _accounts = build_test_router(tmp_path, scopes=CALENDAR_ONLY_SCOPES)
    oauth._next_identity = GoogleIdentity(sub="sub-bob", email="bob@example.com", name="Bob")
    results, done, on_done = _collector()
    router.start_consent("reauth_switch", on_done)
    final = _wait(results, done)
    assert final.status == "success"
    status = router.google_status().data
    assert status["email"] == "bob@example.com"
    assert status["calendar_ready"] and status["gmail_ready"] and status["notes_ready"]
