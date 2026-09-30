"""Regression: pending confirmation must not swallow unrelated commands as cancel."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.helpers_google import build_test_router


def test_gmail_grant_phrase_during_calendar_pending_does_not_cancel(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=11)).strftime("%Y-%m-%d")
    pending = router.calendar_action(action="create", title="Демо", date=future, time="15:00")
    assert pending.status == "confirmation_required"
    op = router.pending.get("sub-alice")
    assert op is not None and op.state == "pending"
    assert op.kind == "calendar_create"

    mistyped = router.handle_text("Дай доступ до Gmail")
    assert mistyped.status == "needs_more_info"
    assert "підтвердження" in mistyped.message.lower() or "очікую" in mistyped.message.lower()
    # Pending create must still be there — not converted to cancel.
    still = router.pending.get("sub-alice")
    assert still is not None
    assert still.op_id == op.op_id
    assert still.kind == "calendar_create"
    assert still.state == "pending"
    assert cal.create_calls == 0
    assert len(cal.events) == 0

    # Subsequent explicit yes still creates the meeting.
    ok = router.handle_text("так")
    assert ok.status == "success"
    assert cal.create_calls == 1


def test_unrelated_command_during_pending_keeps_op(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    future = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=12)).strftime("%Y-%m-%d")
    router.calendar_action(action="create", title="Keep", date=future, time="16:00")
    r = router.handle_text("яка погода завтра")
    assert r.status == "needs_more_info"
    assert router.pending.get("sub-alice") is not None
    assert cal.create_calls == 0
    assert router.handle_text("ні").status == "success"
    assert cal.create_calls == 0
