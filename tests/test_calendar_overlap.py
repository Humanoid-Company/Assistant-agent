"""A taken time slot: the user is asked, the agent never decides or touches the other event."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from assistant import calendar_tool_args
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
SUB = "sub-alice"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=KYIV)
DAY = "2026-10-03"


def _router(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar._now_source = lambda: NOW
    return router, cal


def _seed(cal, event_id: str, summary: str, hm: str, minutes: int) -> None:
    start = datetime.fromisoformat(f"{DAY}T{hm}:00").replace(tzinfo=KYIV)
    cal.events[event_id] = {
        "id": event_id,
        "summary": summary,
        "etag": "etag-1",
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Kyiv"},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(), "timeZone": "Europe/Kyiv"},
    }


def _call(router, args: dict, said: list[str] | None = None):
    return router.calendar_action(**calendar_tool_args(args, session_id="s", user_utterances=said))


def _create(router, hm: str, minutes: int = 90):
    return _call(
        router,
        {"action": "create", "title": "Співбесіда", "date": DAY, "time": hm, "duration_minutes": minutes},
        [f"Створи співбесіду завтра о {hm} на {minutes} хвилин"],
    )


def test_taken_slot_asks_and_keeps_the_other_event(tmp_path):
    router, cal = _router(tmp_path)
    _seed(cal, "biz", "Бізнес", "15:00", 60)

    preview = _create(router, "15:00")
    assert preview.status == "confirmation_required"
    assert "Бізнес" in preview.message and "15:00" in preview.message and "16:00" in preview.message
    assert "інший час" in preview.message
    assert [e["event_id"] for e in preview.data["overlapping_events"]] == ["biz"]
    assert cal.create_calls == 0

    # «Став все одно» → both events exist, the other one is untouched.
    done = _call(router, {"action": "confirm", "confirmation": "так", "op_id": preview.data["op_id"]}, ["так"])
    assert done.status == "success"
    assert cal.create_calls == 1
    assert cal.update_calls == [] and cal.delete_calls == []
    assert cal.events["biz"]["summary"] == "Бізнес"


def test_another_time_is_the_users_choice(tmp_path):
    router, cal = _router(tmp_path)
    _seed(cal, "biz", "Бізнес", "15:00", 60)
    preview = _create(router, "15:00")
    rejected = _call(router, {"action": "confirm", "confirmation": "ні", "op_id": preview.data["op_id"]}, ["ні"])
    assert rejected.status == "success"
    assert cal.create_calls == 0 and cal.update_calls == [] and cal.delete_calls == []


def test_free_and_back_to_back_slots_ask_nothing_extra(tmp_path):
    router, cal = _router(tmp_path)
    _seed(cal, "biz", "Бізнес", "15:00", 60)
    cal.events["holiday"] = {"id": "holiday", "summary": "Свято", "start": {"date": DAY}, "end": {"date": "2026-10-04"}}

    right_after = _create(router, "16:00", 60)  # starts when «Бізнес» ends
    assert right_after.status == "confirmation_required"
    assert "overlapping_events" not in right_after.data
    assert right_after.message.startswith("Створити подію")


def test_long_event_that_started_earlier_counts(tmp_path):
    router, cal = _router(tmp_path)
    _seed(cal, "conf", "Конференція", "09:00", 8 * 60)  # 09:00–17:00
    preview = _create(router, "15:00", 30)
    assert [e["event_id"] for e in preview.data["overlapping_events"]] == ["conf"]


def test_moving_into_a_taken_slot_asks_too(tmp_path):
    router, cal = _router(tmp_path)
    _seed(cal, "biz", "Бізнес", "15:00", 60)
    _seed(cal, "lunch", "Обід", "12:30", 60)

    moved = _call(router, {"action": "reschedule", "event_id": "lunch", "new_date": DAY, "new_time": "15:30"})
    assert moved.status == "confirmation_required"
    assert "Бізнес" in moved.message and "інший час" in moved.message
    assert [e["event_id"] for e in moved.data["overlapping_events"]] == ["biz"]

    # Its own current slot never counts as a clash.
    same = _call(router, {"action": "edit", "event_id": "biz", "duration_minutes": 90})
    assert "overlapping_events" not in (same.data or {})


def test_lookup_failure_does_not_block_creating(tmp_path):
    from integrations.google_errors import GoogleApiError

    router, cal = _router(tmp_path)
    cal.fail_on["list"] = GoogleApiError("server_error", 500, "boom")
    preview = _create(router, "15:00")
    assert preview.status == "confirmation_required"
