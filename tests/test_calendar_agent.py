"""Calendar agent unit tests with FakeCalendarClient."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from integrations.google_errors import GoogleApiError
from tests.helpers_google import build_test_router


def test_create_requires_date_time(tmp_path):
    router, *_ = build_test_router(tmp_path)
    r = router.calendar_action(action="create", title="Стендап")
    assert r.status == "needs_more_info"
    assert "дат" in r.message.lower() or "час" in r.message.lower()


def test_create_confirm_then_success(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=3)).strftime("%Y-%m-%d")
    r1 = router.calendar_action(action="create", title="Демо", date=future, time="14:30", with_meet=True)
    assert r1.status == "confirmation_required"
    assert cal.create_calls == 0

    r2 = router.calendar_action(action="confirm", confirmation="yes")
    assert r2.status == "success"
    assert cal.create_calls == 1
    assert "event_id" in r2.data
    assert "створено" in r2.message.lower() or "готово" in r2.message.lower()


def test_ambiguous_cancel_asks_to_choose(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    for i, hour in enumerate((10, 15)):
        start = (now + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        cal.events[f"e{i}"] = {
            "id": f"e{i}",
            "summary": "Планерка",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
    r = router.calendar_action(action="cancel", query="Планерка")
    assert r.status == "needs_more_info"
    assert "кілька" in r.message.lower()


def test_reject_confirmation_does_not_create(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=2)).strftime("%Y-%m-%d")
    router.calendar_action(action="create", title="X", date=future, time="11:00")
    r = router.calendar_action(action="confirm", confirmation="no")
    assert r.status == "success"
    assert cal.create_calls == 0


def test_duplicate_confirm_after_success_is_idempotent(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=4)).strftime("%Y-%m-%d")
    router.calendar_action(action="create", title="Idem", date=future, time="09:00")
    r1 = router.calendar_action(action="confirm", confirmation="yes")
    r2 = router.calendar_action(action="confirm", confirmation="yes")
    assert r1.status == "success"
    assert r2.status == "success"
    assert cal.create_calls == 1


def test_google_500_does_not_claim_success(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=5)).strftime("%Y-%m-%d")
    router.calendar_action(action="create", title="Fail", date=future, time="16:00")
    cal.fail_with = GoogleApiError("google_unavailable", 500, "Google API тимчасово недоступний.")
    r = router.calendar_action(action="confirm", confirmation="yes")
    assert r.status in ("error", "ambiguous")
    assert "успіх" not in r.message.lower()
    assert "готово" not in r.message.lower()


def test_google_401_asks_reauth(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    cal.fail_with = GoogleApiError("unauthorized", 401, "Google відхилив доступ (401).")
    r = router.calendar_action(action="list")
    assert r.status == "auth_required"


def test_google_429_rate_limit(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    cal.fail_with = GoogleApiError("rate_limited", 429, "Занадто багато запитів до Google (429).")
    r = router.calendar_action(action="list")
    assert r.status == "rate_limited"
    assert "429" in r.message


def test_multistep_context_same_user(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=6)).strftime("%Y-%m-%d")
    r1 = router.handle_text(f"створи зустріч Демо")  # free text → needs more info
    assert r1.status in ("needs_more_info", "confirmation_required")
    r2 = router.calendar_action(action="create", title="Демо", date=future, time="12:00")
    assert r2.status == "confirmation_required"
    r3 = router.handle_text("так")
    assert r3.status == "success"
    assert cal.create_calls == 1
