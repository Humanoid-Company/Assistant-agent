"""Voice-path confirmation and titles the user never said."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from assistant import _model_tool_output, calendar_tool_args
from integrations.google_errors import GoogleApiError
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
FROZEN = datetime(2026, 9, 29, 12, 0, tzinfo=KYIV)
SUB = "sub-alice"
SESSION = "voice-session"
ASK = "А ти можеш записати мені подію завтра на 20?"


def _freeze(router) -> None:
    router.calendar._now_source = lambda: FROZEN


def _create(router, args: dict, utterances: list[str] | None = None, session_id: str = SESSION):
    return router.calendar_action(
        **calendar_tool_args(args, session_id=session_id, user_utterances=utterances)
    )


def test_yes_with_tool_op_id_inserts_once(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = _create(
        router,
        {"action": "create", "title": "Вечеря", "date": "завтра", "time": "20:00"},
        ["Створи подію Вечеря завтра о 20:00"],
    )
    assert preview.status == "confirmation_required"
    payload = json.loads(_model_tool_output(preview))
    assert payload["op_id"] == preview.data["op_id"]
    assert payload["message"] == preview.message
    assert "op_id" not in payload["message"]
    assert cal.create_calls == 0
    done = _create(
        router,
        {"action": "confirm", "confirmation": "Так", "op_id": payload["op_id"]},
        ["Так"],
    )
    assert done.status == "success"
    assert cal.create_calls == 1
    again = _create(router, {"action": "confirm", "confirmation": "yes", "op_id": payload["op_id"]})
    assert again.status == "success"
    assert cal.create_calls == 1


def test_no_and_wrong_op_and_other_session_do_not_insert(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = _create(
        router,
        {"action": "create", "title": "Вечеря", "date": "2026-09-30", "time": "20:00"},
        ["Вечеря завтра о 20:00"],
    )
    rejected = _create(router, {"action": "confirm", "confirmation": "no", "op_id": preview.data["op_id"]})
    assert rejected.status == "success"
    assert cal.create_calls == 0

    preview = _create(
        router,
        {"action": "create", "title": "Вечеря", "date": "2026-09-30", "time": "20:00"},
        ["Вечеря завтра о 20:00"],
    )
    mismatch = _create(
        router,
        {"action": "confirm", "confirmation": "yes", "op_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert mismatch.status == "error"
    assert mismatch.data["reason_code"] == "op_id_mismatch"
    assert mismatch.data["op_match"] is False
    assert mismatch.data["op_id"] == preview.data["op_id"]
    assert "google" not in mismatch.message.lower()
    assert cal.create_calls == 0
    assert router.pending.get(SUB) is not None

    other = _create(
        router,
        {"action": "confirm", "confirmation": "yes", "op_id": preview.data["op_id"]},
        session_id="other-session",
    )
    assert other.status == "error"
    assert other.data["reason_code"] == "session_mismatch"
    assert cal.create_calls == 0


def test_expired_and_missing_pending_have_reason_codes(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    missing = _create(router, {"action": "confirm", "confirmation": "yes"})
    assert missing.status == "error"
    assert missing.data["reason_code"] == "pending_missing"
    assert cal.create_calls == 0

    preview = _create(
        router,
        {"action": "create", "title": "Вечеря", "date": "2026-09-30", "time": "20:00"},
        ["Вечеря о 20:00 завтра"],
    )
    router.pending.get(SUB).expires_at = 0
    expired = _create(router, {"action": "confirm", "confirmation": "yes", "op_id": preview.data["op_id"]})
    assert expired.status == "error"
    assert expired.data["reason_code"] == "pending_expired"
    assert cal.create_calls == 0
    assert router.pending.get(SUB) is None


def test_thanks_does_not_invent_a_title_then_real_name_confirms(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    invented = _create(
        router,
        {"action": "create", "title": "День народження", "date": "завтра", "time": "20:00"},
        [ASK, "Дякую"],
    )
    assert invented.status == "needs_more_info"
    assert invented.data["reason_code"] == "title_not_from_user"
    assert invented.data["missing_fields"] == ["title"]
    assert router.pending.get(SUB) is None
    assert cal.create_calls == 0

    named = _create(
        router,
        {"action": "create", "title": "Вечеря", "date": "2026-09-30", "time": "21:00"},
        [ASK, "Дякую", "Вечеря"],
    )
    assert named.status == "confirmation_required"
    assert "20:00" in named.message
    assert "21:00" not in named.message
    assert router.pending.get(SUB).payload["time"] == "20:00"
    done = _create(router, {"action": "confirm", "confirmation": "yes", "op_id": named.data["op_id"]})
    assert done.status == "success"
    assert cal.create_calls == 1
    assert cal.events[done.data["event_id"]]["summary"] == "Вечеря"


def test_uncertain_google_does_not_insert_again(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = _create(
        router,
        {"action": "create", "title": "Вечеря", "date": "завтра", "time": "20:00"},
        ["Вечеря завтра на 20"],
    )
    cal.raise_after_create = GoogleApiError("timeout", None, "timeout")
    first = _create(router, {"action": "confirm", "confirmation": "yes", "op_id": preview.data["op_id"]})
    assert first.status == "ambiguous"
    assert first.data.get("reason_code") != "op_id_mismatch"
    assert cal.create_calls == 1
    second = _create(router, {"action": "confirm", "confirmation": "yes", "op_id": preview.data["op_id"]})
    assert second.status == "ambiguous"
    assert cal.create_calls == 1
