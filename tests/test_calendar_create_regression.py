"""Create must accept the live Realtime payload, not only title/date/time."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from assistant import _parse_router_reply, calendar_tool_args
from integrations.google_errors import GoogleApiError
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
SUB = "sub-alice"
FROZEN = datetime(2026, 9, 29, 12, 0, tzinfo=KYIV)
LIVE_CREATE = {
    "action": "create",
    "new_start": "2026-09-29T16:00",
    "duration_minutes": 60,
    "new_summary": "Обід",
}


def _freeze(router) -> None:
    router.calendar._now_source = lambda: FROZEN


def _call(router, args: dict, session_id: str = "voice-session"):
    return router.calendar_action(**calendar_tool_args(args, session_id=session_id))


def test_live_new_start_payload_reaches_confirmation(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    result = _call(router, LIVE_CREATE)
    assert result.status == "confirmation_required"
    assert result.data["op_id"]
    assert "коротк" not in result.message.lower()
    assert "Обід" in result.message
    assert "сьогодні" in result.message and "16:00" in result.message
    assert "2026-09-29" not in result.message
    op = router.pending.get(SUB)
    assert op is not None and op.kind == "calendar_create"
    assert op.payload["title"] == "Обід"
    assert op.payload["date"] == "2026-09-29"
    assert op.payload["time"] == "16:00"
    assert op.payload["start"].startswith("2026-09-29T16:00:00+03:00")
    assert cal.create_calls == 0


def test_canonical_title_date_time_with_frozen_clock(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    result = _call(
        router,
        {"action": "create", "title": "Обід", "date": "2026-09-29", "time": "16:00"},
    )
    assert result.status == "confirmation_required"
    op = router.pending.get(SUB)
    assert op.payload["title"] == "Обід"
    assert op.payload["date"] == "2026-09-29"
    assert op.payload["time"] == "16:00"
    assert op.payload["duration_minutes"] == 60
    assert cal.create_calls == 0


def test_confirm_inserts_once_and_reread_matches(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = _call(router, LIVE_CREATE)
    assert cal.create_calls == 0
    done = _call(
        router,
        {"action": "confirm", "confirmation": "yes", "op_id": preview.data["op_id"]},
    )
    assert done.status == "success"
    assert cal.create_calls == 1
    event_id = done.data["event_id"]
    assert event_id in cal.get_calls
    stored = cal.events[event_id]
    assert stored["summary"] == "Обід"
    assert stored["start"]["dateTime"].startswith("2026-09-29T16:00:00+03:00")
    assert stored["end"]["dateTime"].startswith("2026-09-29T17:00:00+03:00")


def test_reject_unclear_and_expired_create_nothing(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = _call(router, LIVE_CREATE)
    assert _call(router, {"action": "confirm", "confirmation": "no", "op_id": preview.data["op_id"]}).status == "success"
    assert cal.create_calls == 0
    assert router.pending.get(SUB) is None

    preview = _call(router, {"action": "create", "title": "Обід у кафе", "date": "2026-09-29", "time": "16:00"})
    noisy = _call(router, {"action": "confirm", "confirmation": "Видловит", "op_id": preview.data["op_id"]})
    assert noisy.status == "needs_more_info"
    assert cal.create_calls == 0
    assert router.pending.get(SUB) is not None
    router.pending.get(SUB).expires_at = 0
    assert _call(router, {"action": "confirm", "confirmation": "yes"}).status == "error"
    assert cal.create_calls == 0
    assert len(cal.events) == 0


def test_short_and_compound_titles_are_both_accepted(tmp_path):
    router, _cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    for title in ("Обід", "Обід у кафе"):
        result = _call(router, {"action": "create", "title": title, "date": "2026-09-30", "time": "16:00"})
        assert result.status == "confirmation_required", title
        assert "коротк" not in result.message.lower()
        assert router.pending.get(SUB).payload["title"] == title
        assert _call(router, {"action": "confirm", "confirmation": "no"}).status == "success"


def test_missing_date_or_time_names_that_field(tmp_path):
    router, _cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    missing_date = _call(router, {"action": "create", "title": "Обід", "time": "16:00"})
    assert missing_date.status == "needs_more_info"
    assert missing_date.data["missing_fields"] == ["date"]
    assert "дата" in missing_date.message.lower()
    assert "назва" not in missing_date.message.lower()
    assert "коротк" not in missing_date.message.lower()
    assert router.pending.get(SUB) is None

    missing_time = _call(router, {"action": "create", "title": "Обід у кафе", "date": "2026-09-29"})
    assert missing_time.status == "needs_more_info"
    assert missing_time.data["missing_fields"] == ["time"]
    assert "час" in missing_time.message.lower()
    assert "назва" not in missing_time.message.lower()
    reply, _awaiting = _parse_router_reply(missing_time)
    assert reply == missing_time.message
    assert "коротк" not in reply.lower()


def test_conflicting_title_and_new_summary_is_not_guessed(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    result = _call(
        router,
        {
            "action": "create",
            "title": "Обід",
            "new_summary": "Обід у кафе",
            "new_start": "2026-09-29T16:00",
        },
    )
    assert result.status == "needs_more_info"
    assert result.data["conflict_fields"] == ["title", "new_summary"]
    assert router.pending.get(SUB) is None
    assert cal.create_calls == 0


def test_edit_move_and_delete_do_not_create(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    start = datetime.now(KYIV) + timedelta(days=2)
    start = start.replace(hour=15, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    cal.events["evt-1"] = {
        "id": "evt-1",
        "summary": "Обід",
        "etag": "etag-1",
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Kyiv"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Kyiv"},
    }
    renamed = _call(router, {"action": "edit", "event_id": "evt-1", "new_summary": "Обід із командою"})
    assert renamed.status == "confirmation_required"
    assert _call(router, {"action": "confirm", "confirmation": "yes", "op_id": renamed.data["op_id"]}).status == "success"
    assert cal.create_calls == 0
    assert cal.events["evt-1"]["summary"] == "Обід із командою"
    assert len(cal.update_calls) == 1

    moved = _call(router, {"action": "reschedule", "event_id": "evt-1", "new_time": "18:00", "new_date": start.strftime("%Y-%m-%d")})
    assert moved.status == "confirmation_required"
    assert _call(router, {"action": "confirm", "confirmation": "yes", "op_id": moved.data["op_id"]}).status == "success"
    assert cal.create_calls == 0
    assert "T18:00" in cal.events["evt-1"]["start"]["dateTime"]

    deleted = _call(router, {"action": "delete", "event_id": "evt-1"})
    assert deleted.status == "confirmation_required"
    assert _call(router, {"action": "confirm", "confirmation": "yes", "op_id": deleted.data["op_id"]}).status == "success"
    assert cal.create_calls == 0
    assert cal.delete_calls == ["evt-1"]
    assert not cal.is_active("evt-1")


def test_concurrent_confirm_and_ambiguous_timeout_create_once(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = _call(router, LIVE_CREATE)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait(timeout=5)
        results.append(_call(router, {"action": "confirm", "confirmation": "yes", "op_id": preview.data["op_id"]}))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert cal.create_calls == 1
    assert len(cal.events) == 1
    assert any(item.status == "success" for item in results)

    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    _call(router, LIVE_CREATE)
    cal.raise_after_create = GoogleApiError("timeout", None, "timeout")
    first = _call(router, {"action": "confirm", "confirmation": "yes"})
    assert first.status == "ambiguous"
    assert cal.create_calls == 1
    second = _call(router, {"action": "confirm", "confirmation": "yes"})
    assert second.status == "ambiguous"
    assert cal.create_calls == 1
    assert "створен" not in second.message.lower()


def test_create_log_has_field_names_not_title_text(tmp_path, caplog):
    router, *_ = build_test_router(tmp_path)
    _freeze(router)
    with caplog.at_level(logging.INFO):
        result = _call(router, LIVE_CREATE)
    assert result.status == "confirmation_required"
    blob = " ".join(record.message for record in caplog.records)
    assert "fields=" in blob
    assert "new_start" in blob and "new_summary" in blob
    assert "Обід" not in blob
    assert "ya29." not in blob
    assert result.data["op_id"] in blob
