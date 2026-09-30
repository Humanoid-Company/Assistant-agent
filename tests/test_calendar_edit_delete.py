"""Regression: find, edit and delete an existing Google Calendar event."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from assistant import TOOLS, calendar_tool_args
from integrations.google_calendar import parse_search_criteria
from integrations.google_errors import GoogleApiError
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
SUB = "sub-alice"


def _aware(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=KYIV)


def _soon(days: int = 2, hm: str = "15:00") -> datetime:
    now = datetime.now(KYIV) + timedelta(days=days)
    hour, minute = hm.split(":")
    return now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)


def seed(
    cal,
    *,
    summary: str = "Обід",
    when: datetime | None = None,
    day: str | None = None,
    hm: str = "15:00",
    duration: int = 60,
    event_id: str = "obid-1",
    description: str = "",
    etag: str = "etag-1",
    **extra,
) -> str:
    start = when or _aware(day or "2026-09-29", hm)
    end = start + timedelta(minutes=duration)
    cal.events[event_id] = {
        "id": event_id,
        "summary": summary,
        "description": description,
        "etag": etag,
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Kyiv"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Kyiv"},
        **extra,
    }
    return event_id


def test_parse_drops_date_and_time_from_title():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=KYIV)
    title, day, clock = parse_search_criteria("Обід 2026-09-29 15:00", now=now, timezone="Europe/Kyiv")
    assert title == "Обід"
    assert day == "2026-09-29"
    assert clock == "15:00"


def test_lunch_query_finds_event_without_putting_date_in_q(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, day="2026-09-29", hm="15:00")
    cal.list_queries.clear()
    found = router.calendar_action(action="cancel", query="Обід 2026-09-29 15:00")
    assert found.status == "confirmation_required"
    assert found.data["event_id"] == eid
    assert router.pending.get(SUB).kind == "calendar_cancel"
    assert cal.list_queries
    for query in cal.list_queries:
        assert query == "Обід"
        assert "2026-09-29" not in (query or "")
        assert "15:00" not in (query or "")
    assert cal.delete_calls == []


def test_search_does_not_create_delete_pending(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    found = router.calendar_action(action="search", query="Обід")
    assert found.status == "success"
    assert "підтверд" not in found.message.lower()
    assert found.data["selected_event_id"] == eid
    assert "event_id" in found.data["events"][0]
    assert found.data["events"][0]["calendar_id"] == "primary"
    assert router.pending.get(SUB) is None
    confirm = router.calendar_action(action="confirm", confirmation="yes")
    assert confirm.status == "error"
    assert eid in cal.events
    assert cal.delete_calls == []


def test_cancel_by_event_id_then_confirm_deletes_once(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    found = router.calendar_action(action="search", query="Обід")
    preview = router.calendar_action(action="cancel", event_id=found.data["selected_event_id"])
    assert preview.status == "confirmation_required"
    assert preview.data["op_id"]
    done = router.calendar_action(action="confirm", confirmation="yes", op_id=preview.data["op_id"])
    assert done.status == "success"
    assert cal.delete_calls == [eid]
    assert not cal.is_active(eid)
    again = router.calendar_action(action="search", query="Обід")
    assert again.status == "not_found"
    replay = router.calendar_action(action="confirm", confirmation="yes")
    assert replay.status == "success"
    assert cal.delete_calls == [eid]


def test_two_same_titles_do_not_delete(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    first = _soon(1, "15:00")
    second = _soon(2, "18:00")
    seed(cal, when=first, event_id="a")
    seed(cal, when=second, event_id="b")
    result = router.calendar_action(action="cancel", query="Обід")
    assert result.status == "needs_more_info"
    assert "кілька" in result.message.lower()
    assert router.pending.get(SUB) is None
    assert router.calendar_action(action="confirm", confirmation="yes").status == "error"
    assert "a" in cal.events and "b" in cal.events


def test_unclear_stt_and_missing_confirmation_do_not_delete(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    preview = router.calendar_action(action="cancel", event_id=eid)
    assert preview.status == "confirmation_required"
    for noise in ("Видловит", "Tam", "maybe"):
        reply = router.calendar_action(action="confirm", confirmation=noise)
        assert reply.status == "needs_more_info"
        assert eid in cal.events
        assert router.pending.get(SUB) is not None
    assert router.calendar_action(action="confirm").status == "needs_more_info"
    assert cal.delete_calls == []
    assert router.calendar_action(action="confirm", confirmation="no").status == "success"
    assert eid in cal.events


def test_wrong_op_session_ttl_and_other_user_do_not_delete(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    preview = router.calendar_action(action="cancel", event_id=eid, session_id="sess-a")
    assert router.calendar_action(action="confirm", confirmation="yes", op_id="wrong").status == "error"
    assert router.calendar_action(
        action="confirm", confirmation="yes", op_id=preview.data["op_id"], session_id="sess-b"
    ).status == "error"
    assert eid in cal.events
    op = router.pending.get(SUB)
    op.expires_at = 0
    assert router.calendar_action(action="confirm", confirmation="yes", session_id="sess-a").status == "error"
    assert eid in cal.events

    router.accounts._active_sub = "sub-bob"
    assert router.calendar_action(action="confirm", confirmation="yes", user_sub=SUB).status == "error"
    assert eid in cal.events
    assert cal.delete_calls == []


def test_concurrent_and_repeat_confirm_delete_once(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    router.calendar_action(action="cancel", event_id=eid)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait(timeout=5)
        results.append(router.calendar_action(action="confirm", confirmation="yes"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert cal.delete_calls == [eid]
    assert not cal.is_active(eid)
    assert any(result.status == "success" for result in results)
    repeat = router.calendar_action(action="confirm", confirmation="yes")
    assert repeat.status == "success"
    assert cal.delete_calls == [eid]


def test_delete_api_errors_do_not_claim_success_or_retry(tmp_path):
    cases = (
        ("forbidden", 403, "permission_denied"),
        ("not_found", 404, "not_found"),
        ("rate_limited", 429, "rate_limited"),
        ("google_unavailable", 500, "error"),
        ("timeout", None, "error"),
    )
    for code, status, expected in cases:
        router, cal, *_ = build_test_router(tmp_path)
        eid = seed(cal, when=_soon(1), event_id=f"e-{code}")
        router.calendar_action(action="cancel", event_id=eid)
        if code == "not_found":
            del cal.events[eid]
        else:
            cal.fail_on["delete"] = GoogleApiError(code, status, f"Google {code}")
        result = router.calendar_action(action="confirm", confirmation="yes")
        assert result.status == expected, (code, result.status, result.message)
        assert "скасовано" not in result.message.lower()
        assert result.status != "success"
        if expected == "error" and code in ("timeout", "google_unavailable"):
            assert result.data.get("verify") == "still_present"
            assert "все ще є" in result.message.lower()
        calls = len(cal.delete_calls)
        again = router.calendar_action(action="confirm", confirmation="yes")
        assert again.status != "success"
        assert len(cal.delete_calls) == calls
        if code != "not_found":
            assert eid in cal.events


def test_delete_timeout_after_remove_is_reconciled_as_success(tmp_path):
    """raise_after_delete: event already gone — reconcile via get → success, no second delete."""
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), summary="Сніданок")
    router.calendar_action(action="cancel", event_id=eid)
    cal.raise_after_delete = GoogleApiError("timeout", None, "timeout")
    first = router.calendar_action(action="confirm", confirmation="yes")
    assert first.status == "success"
    assert first.data.get("verify") in {"absent", "cancelled"}
    assert "видалено" in first.message.lower()
    assert not cal.is_active(eid)
    assert cal.delete_calls == [eid]
    second = router.calendar_action(action="confirm", confirmation="yes")
    assert second.status == "success"
    assert cal.delete_calls == [eid]


def test_delete_uncertain_while_event_still_present_does_not_retry(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), summary="Сніданок")
    router.calendar_action(action="cancel", event_id=eid)
    cal.fail_on["delete"] = GoogleApiError("timeout", None, "timeout")
    first = router.calendar_action(action="confirm", confirmation="yes")
    assert first.status == "error"
    assert first.data.get("verify") == "still_present"
    assert "все ще є" in first.message.lower()
    assert eid in cal.events
    # New cancel must not be blocked forever — pending cleared after verified failure.
    again = router.calendar_action(action="cancel", event_id=eid)
    assert again.status == "confirmation_required"



def test_move_lunch_preserves_duration_and_does_not_create(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, day="2026-09-29", hm="15:00", duration=60, description="тайне")
    cal.list_queries.clear()
    preview = router.calendar_action(
        action="reschedule",
        query="Обід 2026-09-29 15:00 на 16:00",
        new_time="16:00",
    )
    assert preview.status == "confirmation_required"
    assert "16:00" in preview.message
    assert "15:00" in preview.message
    assert "підтвердж" in preview.message.lower()
    assert router.pending.get(SUB).kind == "calendar_reschedule"
    assert cal.list_queries[-1] == "Обід"
    assert "16:00" not in (cal.list_queries[-1] or "")
    assert "15:00" not in (cal.list_queries[-1] or "")
    done = router.calendar_action(action="confirm", confirmation="yes")
    assert done.status == "success"
    assert done.data["event_id"] == eid
    assert cal.create_calls == 0
    assert cal.delete_calls == []
    assert len(cal.update_calls) == 1
    assert cal.update_calls[0][0] == eid
    body = cal.update_calls[0][1]
    assert "summary" not in body
    assert "description" not in body
    start = _aware("2026-09-29", "16:00")
    assert cal.events[eid]["start"]["dateTime"] == start.isoformat()
    assert cal.events[eid]["end"]["dateTime"] == (start + timedelta(hours=1)).isoformat()
    assert cal.events[eid]["summary"] == "Обід"
    assert cal.events[eid]["description"] == "тайне"


def test_move_to_another_day_title_duration_and_description(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    origin = _soon(2, "15:00")
    eid = seed(cal, when=origin, description="старий", duration=60)
    target_day = (origin + timedelta(days=1)).strftime("%Y-%m-%d")

    moved = router.calendar_action(
        action="edit", query="Обід", new_date=target_day, new_time="14:30"
    )
    assert moved.status == "confirmation_required"
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    assert cal.events[eid]["start"]["dateTime"].startswith(f"{target_day}T14:30")
    assert cal.events[eid]["end"]["dateTime"].startswith(f"{target_day}T15:30")
    assert cal.events[eid]["summary"] == "Обід"
    assert cal.events[eid]["description"] == "старий"
    assert cal.create_calls == 0
    assert cal.delete_calls == []

    renamed = router.calendar_action(action="edit", event_id=eid, new_summary="Обід із командою")
    assert renamed.status == "confirmation_required"
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    assert cal.events[eid]["summary"] == "Обід із командою"
    assert cal.events[eid]["start"]["dateTime"].startswith(f"{target_day}T14:30")
    assert "start" not in cal.update_calls[-1][1]

    longer = router.calendar_action(action="edit", event_id=eid, duration_minutes=90)
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    start = datetime.fromisoformat(cal.events[eid]["start"]["dateTime"])
    end = datetime.fromisoformat(cal.events[eid]["end"]["dateTime"])
    assert int((end - start).total_seconds() // 60) == 90
    assert "start" not in cal.update_calls[-1][1]
    assert cal.events[eid]["summary"] == "Обід із командою"

    described = router.calendar_action(action="edit", event_id=eid, new_description="порядок денний")
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    assert cal.events[eid]["description"] == "порядок денний"
    assert set(cal.update_calls[-1][1]) == {"description"}
    assert len(cal.events) == 1


def test_selected_event_is_reused_for_this_event(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    found = router.calendar_action(action="search", query="Обід")
    assert found.data["selected_event_id"] == eid
    cal.list_queries.clear()
    preview = router.calendar_action(action="edit", query="цю подію", new_summary="Обід із командою")
    assert preview.status == "confirmation_required"
    assert preview.data["event_id"] == eid
    assert cal.list_queries == []
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    assert cal.events[eid]["summary"] == "Обід із командою"
    assert len(cal.update_calls) == 1


def test_invalid_schedule_creates_no_pending(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    cases = (
        {"new_date": "2099-02-30", "new_time": "15:00"},
        {"new_date": "2099-06-01", "new_time": "25:90"},
        {"new_start": "2099-06-01T18:00", "new_end": "2099-06-01T18:00"},
        {"new_date": "2026-03-29", "new_time": "03:30"},
    )
    for fields in cases:
        result = router.calendar_action(action="edit", event_id=eid, **fields)
        assert result.status in ("needs_more_info", "error"), fields
        assert router.pending.get(SUB) is None
        assert router.calendar_action(action="confirm", confirmation="yes").status == "error"
        assert cal.update_calls == []
        assert eid in cal.events


def test_edit_reject_repeat_and_concurrent_confirm(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(3))
    original = cal.events[eid]["start"]["dateTime"]
    future = (datetime.now(KYIV) + timedelta(days=9)).strftime("%Y-%m-%d")
    preview = router.calendar_action(action="edit", event_id=eid, new_date=future, new_time="16:00")
    assert router.calendar_action(action="confirm", confirmation="no").status == "success"
    assert cal.events[eid]["start"]["dateTime"] == original
    assert cal.update_calls == []

    preview = router.calendar_action(action="edit", event_id=eid, new_date=future, new_time="16:00")
    assert router.calendar_action(action="confirm", confirmation="yes", op_id=preview.data["op_id"]).status == "success"
    assert len(cal.update_calls) == 1
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    assert len(cal.update_calls) == 1

    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(4))
    router.calendar_action(action="edit", event_id=eid, new_summary="Інша")
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait(timeout=5)
        results.append(router.calendar_action(action="confirm", confirmation="yes"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(cal.update_calls) == 1
    assert cal.events[eid]["summary"] == "Інша"
    assert any(result.status == "success" for result in results)


def test_expired_wrong_op_and_bob_do_not_edit(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(2))
    original = cal.events[eid]["summary"]
    preview = router.calendar_action(
        action="edit", event_id=eid, new_summary="Не чіпати", session_id="alice-session"
    )
    assert router.calendar_action(action="confirm", confirmation="yes", op_id="nope").status == "error"
    assert router.calendar_action(
        action="confirm", confirmation="yes", session_id="bob-session", op_id=preview.data["op_id"]
    ).status == "error"
    op = router.pending.get(SUB)
    op.expires_at = 0
    assert router.calendar_action(action="confirm", confirmation="yes", session_id="alice-session").status == "error"
    router.accounts._active_sub = "sub-bob"
    assert router.calendar_action(action="confirm", confirmation="yes", user_sub=SUB).status == "error"
    assert cal.update_calls == []
    assert cal.events[eid]["summary"] == original


def test_stale_etag_asks_again_and_does_not_patch(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(2), etag="etag-1")
    router.calendar_action(action="edit", event_id=eid, new_summary="Нова")
    cal.events[eid]["etag"] = "etag-changed"
    cal.events[eid]["summary"] = "Змінено зовні"
    result = router.calendar_action(action="confirm", confirmation="yes")
    assert result.status == "needs_more_info"
    assert cal.update_calls == []
    assert cal.events[eid]["summary"] == "Змінено зовні"
    assert router.calendar_action(action="confirm", confirmation="yes").status == "error"


def test_edit_api_errors_do_not_retry(tmp_path):
    cases = (
        ("forbidden", 403, "permission_denied"),
        ("not_found", 404, "not_found"),
        ("rate_limited", 429, "rate_limited"),
        ("google_unavailable", 500, "ambiguous"),
        ("timeout", None, "ambiguous"),
    )
    for code, status, expected in cases:
        router, cal, *_ = build_test_router(tmp_path)
        eid = seed(cal, when=_soon(1), event_id=f"e-{code}")
        original = cal.events[eid]["summary"]
        router.calendar_action(action="edit", event_id=eid, new_summary="Нова")
        if code == "not_found":
            del cal.events[eid]
        else:
            cal.fail_on["update"] = GoogleApiError(code, status, f"Google {code}")
        result = router.calendar_action(action="confirm", confirmation="yes")
        assert result.status == expected, code
        assert result.status != "success"
        calls = len(cal.update_calls)
        again = router.calendar_action(action="confirm", confirmation="yes")
        assert again.status != "success"
        assert len(cal.update_calls) == calls
        if code != "not_found":
            assert cal.events[eid]["summary"] == original


def test_edit_timeout_after_patch_is_not_repeated(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    router.calendar_action(action="edit", event_id=eid, new_summary="Вже змінено")
    cal.raise_after_update = GoogleApiError("timeout", None, "timeout")
    first = router.calendar_action(action="confirm", confirmation="yes")
    assert first.status == "ambiguous"
    assert cal.events[eid]["summary"] == "Вже змінено"
    assert len(cal.update_calls) == 1
    second = router.calendar_action(action="confirm", confirmation="yes")
    assert second.status == "ambiguous"
    assert len(cal.update_calls) == 1


def test_recurring_without_scope_is_not_mutated(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    master = seed(cal, when=_aware("2020-01-01", "15:00"), event_id="series-master")
    cal.events[master]["recurrence"] = ["RRULE:FREQ=DAILY"]
    instance = seed(
        cal,
        day="2026-09-29",
        hm="15:00",
        event_id="series-instance",
        recurringEventId="series-master",
    )
    cancel = router.calendar_action(action="cancel", query="Обід 2026-09-29 15:00")
    assert cancel.status == "needs_more_info"
    assert "сері" in cancel.message.lower() or "екземпляр" in cancel.message.lower()
    assert router.pending.get(SUB) is None
    assert router.calendar_action(action="confirm", confirmation="yes").status == "error"
    assert instance in cal.events and master in cal.events

    edit = router.calendar_action(
        action="edit", event_id=instance, new_time="16:00", new_date="2026-09-29"
    )
    assert edit.status == "needs_more_info"
    assert cal.update_calls == []

    scoped = router.calendar_action(
        action="cancel", event_id=instance, recurrence_scope="instance"
    )
    assert scoped.status == "confirmation_required"
    assert router.calendar_action(action="confirm", confirmation="yes").status == "success"
    assert cal.delete_calls == [instance]
    assert not cal.is_active(instance)
    assert master in cal.events


def test_tool_schema_e2e_list_edit_and_delete(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    schema = next(tool for tool in TOOLS if tool["name"] == "calendar_action")
    props = schema["parameters"]["properties"]
    for field in (
        "event_id",
        "calendar_id",
        "title",
        "date",
        "time",
        "new_start",
        "new_end",
        "duration_minutes",
        "new_summary",
        "new_description",
        "op_id",
    ):
        assert field in props
    assert "edit" in props["action"]["enum"]
    when = _soon(1, "15:00")
    eid = seed(cal, when=when, description="секрет листа не тут")

    listed = router.calendar_action(
        **calendar_tool_args({"action": "list", "user_sub": "sub-bob", "email": "a@b.c"}, session_id="sess")
    )
    assert listed.status == "success"
    assert listed.data["selected_event_id"] == eid
    preview = router.calendar_action(
        **calendar_tool_args(
            {
                "action": "edit",
                "query": "цю подію",
                "new_time": "16:00",
                "session_id": "forged",
            },
            session_id="sess",
        )
    )
    assert preview.status == "confirmation_required"
    assert preview.data["event_id"] == eid
    assert "підтвердж" in preview.message.lower()
    done = router.calendar_action(
        **calendar_tool_args(
            {"action": "confirm", "confirmation": "yes", "op_id": preview.data["op_id"]},
            session_id="sess",
        )
    )
    assert done.status == "success"
    assert cal.events[eid]["start"]["dateTime"].endswith("16:00:00+03:00") or "T16:00" in cal.events[eid]["start"]["dateTime"]
    assert len(cal.update_calls) == 1
    assert cal.create_calls == 0

    cancel = router.calendar_action(
        **calendar_tool_args({"action": "delete", "event_id": eid}, session_id="sess")
    )
    assert cancel.status == "confirmation_required"
    deleted = router.calendar_action(
        **calendar_tool_args(
            {"action": "confirm", "confirmation": "yes", "op_id": cancel.data["op_id"]},
            session_id="sess",
        )
    )
    assert deleted.status == "success"
    assert not cal.is_active(eid)
    assert cal.delete_calls == [eid]


def test_side_request_and_logs_do_not_touch_pending_or_secrets(tmp_path, caplog):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), description="SECRET_DESC_TOKEN")
    with caplog.at_level(logging.INFO):
        preview = router.calendar_action(action="cancel", event_id=eid)
        side = router.handle_text("Дай доступ до Gmail")
        noisy = router.handle_text("Видловит")
    assert side.status == "needs_more_info"
    assert noisy.status == "needs_more_info"
    assert router.pending.get(SUB).op_id == preview.data["op_id"]
    assert router.pending.get(SUB).kind == "calendar_cancel"
    blob = " ".join(record.message for record in caplog.records)
    assert preview.data["op_id"] in blob
    assert "calendar_cancel" in blob or "cancel" in blob
    assert "SECRET_DESC_TOKEN" not in blob
    assert "ya29." not in blob
    assert "1//" not in blob
    assert eid in cal.events


def test_foreign_calendar_id_is_rejected(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1))
    result = router.calendar_action(action="cancel", event_id=eid, calendar_id="other@gmail.com")
    assert result.status == "error"
    assert router.pending.get(SUB) is None
    assert cal.delete_calls == []
    assert eid in cal.events
