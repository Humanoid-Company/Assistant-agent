"""Regression: reschedule must not create cancel-pending on invalid dates."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from integrations.google_errors import GoogleApiError
from tests.helpers_google import build_test_router


def _seed_event(cal, summary: str = "Test") -> str:
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    start = (now + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    eid = "evt-test-1"
    cal.events[eid] = {
        "id": eid,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    return eid


def test_invalid_date_feb30_leaves_pending_empty(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = _seed_event(cal)
    r = router.calendar_action(
        action="reschedule",
        event_id=eid,
        new_date="2099-02-30",
        new_time="15:00",
    )
    assert r.status in ("needs_more_info", "error")
    assert router.pending.get("sub-alice") is None
    # Subsequent confirm must not cancel/delete
    confirm = router.calendar_action(action="confirm", confirmation="yes")
    assert confirm.status == "error"
    assert eid in cal.events


def test_invalid_time_25_90_leaves_pending_empty(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = _seed_event(cal)
    r = router.calendar_action(
        action="reschedule",
        event_id=eid,
        new_date="2099-06-01",
        new_time="25:90",
    )
    assert r.status == "needs_more_info"
    assert router.pending.get("sub-alice") is None
    assert router.calendar_action(action="confirm", confirmation="yes").status == "error"
    assert eid in cal.events


def test_valid_reschedule_moves_once(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = _seed_event(cal)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=10)).strftime("%Y-%m-%d")
    r1 = router.calendar_action(action="reschedule", event_id=eid, new_date=future, new_time="16:30")
    assert r1.status == "confirmation_required"
    op = router.pending.get("sub-alice")
    assert op is not None
    assert op.kind == "calendar_reschedule"
    assert op.payload["event_id"] == eid

    r2 = router.calendar_action(action="confirm", confirmation="yes")
    assert r2.status == "success"
    assert cal.events[eid]["start"]["dateTime"].startswith(f"{future}T16:30")


def test_reschedule_reject_modifies_nothing(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = _seed_event(cal)
    original = cal.events[eid]["start"]["dateTime"]
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=11)).strftime("%Y-%m-%d")
    router.calendar_action(action="reschedule", event_id=eid, new_date=future, new_time="11:00")
    r = router.calendar_action(action="confirm", confirmation="no")
    assert r.status == "success"
    assert cal.events[eid]["start"]["dateTime"] == original
    assert router.pending.get("sub-alice") is None


def test_reschedule_duplicate_confirm_not_twice(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = _seed_event(cal)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=12)).strftime("%Y-%m-%d")
    router.calendar_action(action="reschedule", event_id=eid, new_date=future, new_time="09:15")
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    first_start = cal.events[eid]["start"]["dateTime"]
    # Force a second update would change payload — duplicate confirm should be idempotent
    r2 = router.calendar_action(action="confirm", confirmation="yes")
    assert r2.status == "success"
    assert cal.events[eid]["start"]["dateTime"] == first_start


def test_reschedule_api_failure_no_success_claim(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = _seed_event(cal)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=13)).strftime("%Y-%m-%d")
    router.calendar_action(action="reschedule", event_id=eid, new_date=future, new_time="18:00")
    cal.fail_with = GoogleApiError("google_unavailable", 500, "Google API тимчасово недоступний.")
    r = router.calendar_action(action="confirm", confirmation="yes")
    assert r.status == "error"
    assert "готово" not in r.message.lower()
    assert "успіш" not in r.message.lower()
    assert "створен" not in r.message.lower()


def test_pending_expires_blocks_confirm(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.pending._ttl = 0.01
    eid = _seed_event(cal)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=14)).strftime("%Y-%m-%d")
    r1 = router.calendar_action(action="reschedule", event_id=eid, new_date=future, new_time="12:00")
    assert r1.status == "confirmation_required"
    # Manually expire
    op = router.pending._by_user.get("sub-alice")
    assert op is not None
    op.expires_at = 0
    r2 = router.calendar_action(action="confirm", confirmation="yes")
    assert r2.status == "error"
    assert eid in cal.events
