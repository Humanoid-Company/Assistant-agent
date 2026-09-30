"""Empty or invalid calendar_action must not raise or touch Google."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from assistant import TOOLS, _parse_router_reply, _router_tool_failure_message, calendar_tool_args
from realtime_client import parse_tool_arguments
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
FROZEN = datetime(2026, 9, 29, 12, 0, tzinfo=KYIV)


def _freeze(router) -> None:
    router.calendar._now_source = lambda: FROZEN


def _block_handle(router) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("handle must not run")

    router.calendar.handle = boom


def test_schema_requires_action_and_matches_realtime_shape():
    schema = next(tool for tool in TOOLS if tool["name"] == "calendar_action")
    assert schema["type"] == "function"
    assert schema["name"] == "calendar_action"
    assert "parameters" in schema
    assert schema["parameters"]["required"] == ["action"]
    assert "create" in schema["parameters"]["properties"]["action"]["enum"]


def test_empty_call_is_needs_more_info_and_skips_handle(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _block_handle(router)
    result = router.calendar_action(**calendar_tool_args({}))
    assert result.status == "needs_more_info"
    assert result.data["missing_fields"] == ["action"]
    assert "google" not in result.message.lower()
    reply, awaiting = _parse_router_reply(result)
    assert reply == result.message
    assert awaiting is True
    assert cal.create_calls == 0
    assert cal.list_queries == []


def test_missing_and_unknown_action_do_not_call_google(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _block_handle(router)
    missing = router.calendar_action()
    assert missing.status == "needs_more_info"
    assert missing.data["missing_fields"] == ["action"]
    unknown = router.calendar_action(action="explode")
    assert unknown.status == "needs_more_info"
    assert unknown.data["invalid_fields"] == ["action"]
    assert "google" not in unknown.message.lower()
    assert cal.create_calls == 0
    assert cal.list_queries == []


def test_wrong_types_do_not_raise(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _block_handle(router)
    result = router.calendar_action(
        action="create",
        title="Вечеря",
        duration_minutes=True,
        confirmation=1,
    )
    assert result.status == "needs_more_info"
    assert "duration_minutes" in result.data["invalid_fields"]
    assert "confirmation" in result.data["invalid_fields"]
    assert "google" not in result.message.lower()
    assert cal.create_calls == 0


def test_choice_of_names_asks_instead_of_creating(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    result = router.calendar_action(
        **calendar_tool_args(
            {
                "action": "create",
                "new_summary": "Обід або вечеря",
                "new_start": "2026-09-29T18:00",
                "duration_minutes": 60,
            }
        )
    )
    assert result.status == "needs_more_info"
    assert result.data["missing_fields"] == ["title"]
    assert "назв" in result.message.lower()
    assert router.pending.get("sub-alice") is None
    assert cal.create_calls == 0


def test_dinner_tomorrow_confirms_once_and_reject_writes_nothing(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = router.calendar_action(
        **calendar_tool_args(
            {"action": "create", "title": "Вечеря", "date": "завтра", "time": "18:00"}
        )
    )
    assert preview.status == "confirmation_required"
    assert preview.data["op_id"]
    assert "Вечеря" in preview.message and "18:00" in preview.message
    op = router.pending.get("sub-alice")
    assert op.payload["date"] == "2026-09-30"
    assert op.payload["time"] == "18:00"
    assert cal.create_calls == 0

    rejected = router.calendar_action(
        action="confirm", confirmation="no", op_id=preview.data["op_id"]
    )
    assert rejected.status == "success"
    assert cal.create_calls == 0
    assert len(cal.events) == 0

    again = router.calendar_action(
        action="create", title="Вечеря", date="2026-09-30", time="18:00", duration_minutes=60
    )
    done = router.calendar_action(action="confirm", confirmation="yes", op_id=again.data["op_id"])
    assert done.status == "success"
    assert cal.create_calls == 1
    replay = router.calendar_action(action="confirm", confirmation="yes", op_id=again.data["op_id"])
    assert replay.status == "success"
    assert cal.create_calls == 1
    assert len(cal.events) == 1


def test_missing_create_fields_name_the_gap(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    missing = router.calendar_action(action="create", time="18:00")
    assert missing.status == "needs_more_info"
    assert missing.data["missing_fields"] == ["title", "date"]
    assert "google" not in missing.message.lower()
    assert router.pending.get("sub-alice") is None
    assert cal.create_calls == 0


def test_search_edit_and_delete_still_work(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    start = datetime.now(KYIV) + timedelta(days=2)
    start = start.replace(hour=15, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    cal.events["evt-1"] = {
        "id": "evt-1",
        "summary": "Вечеря",
        "etag": "etag-1",
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Kyiv"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Kyiv"},
    }
    found = router.calendar_action(action="search", title="Вечеря")
    assert found.status == "success"
    renamed = router.calendar_action(action="edit", event_id="evt-1", new_summary="Пізня вечеря")
    assert renamed.status == "confirmation_required"
    assert router.calendar_action(action="confirm", confirmation="yes", op_id=renamed.data["op_id"]).status == "success"
    assert cal.create_calls == 0
    assert cal.events["evt-1"]["summary"] == "Пізня вечеря"
    deleted = router.calendar_action(action="delete", event_id="evt-1")
    assert router.calendar_action(action="confirm", confirmation="yes", op_id=deleted.data["op_id"]).status == "success"
    assert not cal.is_active("evt-1")
    assert cal.create_calls == 0


def test_parse_tool_arguments_hides_values(caplog):
    with caplog.at_level(logging.INFO):
        assert parse_tool_arguments("") == {}
        assert parse_tool_arguments("{") == {}
        secret = '{"new_summary":"Обід","token":"ya29.secret"}'
        parsed = parse_tool_arguments(secret)
    assert parsed["new_summary"] == "Обід"
    blob = " ".join(record.message for record in caplog.records)
    assert "Обід" not in blob
    assert "ya29" not in blob
    assert "tool arguments empty" in blob
    assert "invalid json" in blob


def test_type_error_is_not_described_as_google():
    text = _router_tool_failure_message(TypeError("missing action"))
    assert "google" not in text.lower()
    assert "google" in _router_tool_failure_message(RuntimeError("api")).lower()
