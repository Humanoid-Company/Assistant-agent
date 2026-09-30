"""Regression: concurrent / repeated confirm must never double-mutate Google."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from integrations.google_errors import GoogleApiError
from tests.helpers_google import build_test_router


def _future_date(days: int = 7) -> str:
    return (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=days)).strftime("%Y-%m-%d")


def test_two_concurrent_confirms_create_one_event(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    r = router.calendar_action(action="create", title="Race", date=_future_date(8), time="10:00")
    assert r.status == "confirmation_required"

    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        barrier.wait(timeout=5)
        results.append(router.calendar_action(action="confirm", confirmation="yes"))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert cal.create_calls == 1
    assert len(cal.events) == 1
    statuses = {r.status for r in results}
    assert "success" in statuses
    # Loser may report success (replay) or "already executing" / error — never a second create.
    assert all(r.status in ("success", "error") for r in results)


def test_reconfirm_after_success_no_duplicate(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar_action(action="create", title="Once", date=_future_date(9), time="11:00")
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    again = router.calendar_action(action="confirm", confirmation="yes")
    assert again.status == "success"
    assert cal.create_calls == 1
    assert len(cal.events) == 1


def test_timeout_after_google_create_does_not_duplicate_on_retry(tmp_path):
    """API created the event, then network failed — second yes must not insert again."""
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar_action(action="create", title="Ambiguous", date=_future_date(10), time="12:00")
    cal.raise_after_create = GoogleApiError("timeout", None, "Мережевий timeout після створення.")
    first = router.calendar_action(action="confirm", confirmation="yes")
    assert first.status in ("error", "ambiguous")
    assert cal.create_calls == 1
    assert len(cal.events) == 1

    second = router.calendar_action(action="confirm", confirmation="yes")
    assert second.status in ("error", "ambiguous")
    assert "не повтор" in second.message.lower() or "перевір" in second.message.lower()
    assert cal.create_calls == 1
    assert len(cal.events) == 1


def test_concurrent_gmail_confirm_sends_once(tmp_path):
    router, _cal, mail, *_ = build_test_router(tmp_path)
    router.gmail_action(action="send", to="a@example.com", subject="Hi", body="Body")
    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        barrier.wait(timeout=5)
        results.append(router.gmail_action(action="confirm", confirmation="yes"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(mail.sent) == 1


def test_gmail_timeout_after_send_no_resend(tmp_path):
    router, _cal, mail, *_ = build_test_router(tmp_path)
    router.gmail_action(action="send", to="a@example.com", subject="Hi", body="Body")
    mail.raise_after_send = GoogleApiError("network", None, "Зв'язок обірвався.")
    first = router.gmail_action(action="confirm", confirmation="yes")
    assert first.status == "error"
    assert len(mail.sent) == 1
    second = router.gmail_action(action="confirm", confirmation="yes")
    assert second.status == "error"
    assert len(mail.sent) == 1
