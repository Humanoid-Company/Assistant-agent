"""Silence after calendar ops, ambiguous cancel reconciliation, no repeat cancel on praise."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agents.types import AgentResult
from assistant import (
    _model_tool_output,
    _parse_router_reply,
    _should_speak_router_result,
    calendar_tool_args,
)
from integrations.google_errors import GoogleApiError
from realtime_client import RealtimeConversation
from tests.helpers_google import build_test_router
from tests.test_calendar_edit_delete import _soon, seed

KYIV = ZoneInfo("Europe/Kyiv")
SUB = "sub-alice"
SESSION = "voice-session"


def _confirm(router, op_id: str | None = None, text: str = "Так"):
    args = {"action": "confirm", "confirmation": text}
    if op_id:
        args["op_id"] = op_id
    return router.calendar_action(**calendar_tool_args(args, session_id=SESSION))


def test_terminal_calendar_statuses_must_be_spoken():
    for status in ("success", "ambiguous", "not_found", "error", "confirmation_required"):
        result = AgentResult(status, f"msg-{status}", {"op_id": "x"})
        assert _should_speak_router_result(result) is True
        reply, awaiting = _parse_router_reply(result)
        assert reply.startswith("msg-")
        if status == "confirmation_required":
            assert awaiting is True


def test_ambiguous_then_note_emotion_still_gets_say_fallback():
    """Reproduce: confirm → ambiguous JSON + note_emotion silence → must still speak."""
    spoken: list[str] = []
    sent: list[dict] = []
    conv = RealtimeConversation(
        tools=[],
        on_tool_call=lambda name, args, call_id: "" if name == "note_emotion" else None,
        silent_tools={"note_emotion"},
    )
    conv._send = sent.append  # type: ignore[method-assign]
    conv.say = lambda text: spoken.append(text)  # type: ignore[method-assign]

    result = AgentResult(
        "ambiguous",
        "Не можу точно підтвердити видалення. Зараз не вдалося перевірити стан календаря.",
        {"op_id": "op-1", "pending_state": "ambiguous", "kind": "calendar_cancel"},
    )
    assert _should_speak_router_result(result) is True
    output = _model_tool_output(result)
    conv.submit_deferred_tool_result(call_id="c1", output=output, trigger_followup=False, allow_tool_calls=False)
    conv.say(result.message)

    # Silent emotion-only turn would force a follow-up — speech already delivered via say().
    from types import SimpleNamespace

    conv._silent_tool_used_this_response = True
    conv._responses_requested = 1
    conv._handle_event(SimpleNamespace(type="response.done", response=SimpleNamespace(status="completed")))
    assert spoken == [result.message]
    assert json.loads(output)["status"] == "ambiguous"
    assert any(item.get("type") == "conversation.item.create" for item in sent)


def test_successful_delete_spoken_message(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), summary="Сніданок", event_id="breakfast-1")
    preview = router.calendar_action(action="cancel", event_id=eid, session_id=SESSION)
    assert preview.status == "confirmation_required"
    done = _confirm(router, preview.data["op_id"])
    assert done.status == "success"
    assert done.data.get("verify") == "cancelled"
    assert "видалено" in done.message.lower() or "скасовано" in done.message.lower()
    assert not cal.is_active(eid)
    assert _should_speak_router_result(done) is True


def test_delete_success_when_google_returns_cancelled_tombstone(tmp_path):
    """Live Google soft-deletes: get() after delete returns status=cancelled, not 404."""
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), summary="повечеряти", event_id="dinner-tombstone")
    preview = router.calendar_action(action="cancel", event_id=eid, session_id=SESSION)
    done = _confirm(router, preview.data["op_id"])
    assert done.status == "success"
    assert done.data.get("verify") == "cancelled"
    assert "все ще є" not in done.message.lower()
    assert eid in cal.events
    assert cal.events[eid]["status"] == "cancelled"
    assert not cal.is_active(eid)


def test_ambiguous_delete_reconciles_when_event_gone(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), summary="Сніданок", event_id="breakfast-2")
    preview = router.calendar_action(action="cancel", event_id=eid, session_id=SESSION)
    cal.raise_after_delete = GoogleApiError("timeout", None, "timeout")
    done = _confirm(router, preview.data["op_id"])
    assert done.status == "success"
    assert done.data["verify"] in {"absent", "cancelled"}
    assert "сніданок" in done.message.casefold() or "видалено" in done.message.lower()
    assert cal.delete_calls == [eid]
    # Repeat confirm is idempotent — no second delete.
    again = _confirm(router, preview.data["op_id"])
    assert again.status == "success"
    assert cal.delete_calls == [eid]


def test_ambiguous_delete_reports_still_present(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), summary="Сніданок", event_id="breakfast-3")
    preview = router.calendar_action(action="cancel", event_id=eid, session_id=SESSION)
    cal.fail_on["delete"] = GoogleApiError("google_unavailable", 503, "down")
    done = _confirm(router, preview.data["op_id"])
    assert done.status == "error"
    assert done.data["verify"] == "still_present"
    assert "все ще є" in done.message.lower()
    assert eid in cal.events
    # Attempt recorded, but event must remain.
    assert eid in cal.delete_calls
    assert eid in cal.events


def test_ambiguous_delete_unresolved_when_get_also_fails(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), summary="Сніданок", event_id="breakfast-4")
    preview = router.calendar_action(action="cancel", event_id=eid, session_id=SESSION)
    cal.raise_after_delete = GoogleApiError("timeout", None, "timeout")
    # After propose, next get is pre-delete; the one after that is reconcile.
    cal.fail_get_on_call = len(cal.get_calls) + 2
    done = _confirm(router, preview.data["op_id"])
    assert done.status == "ambiguous"
    assert done.data["verify"] == "unresolved"
    assert "не можу точно підтвердити" in done.message.lower()
    # Must not delete again on a second confirm while get still fails.
    cal.fail_get_on_call = len(cal.get_calls) + 1
    again = _confirm(router, preview.data["op_id"])
    assert again.status == "ambiguous"
    assert cal.delete_calls == [eid]


def test_praise_does_not_start_second_cancel_while_ambiguous(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), summary="Сніданок", event_id="breakfast-5")
    preview = router.calendar_action(action="cancel", event_id=eid, session_id=SESSION)
    cal.raise_after_delete = GoogleApiError("timeout", None, "timeout")
    cal.fail_get_on_call = len(cal.get_calls) + 2
    first = _confirm(router, preview.data["op_id"])
    assert first.status == "ambiguous"
    assert router.pending.get(SUB).state == "ambiguous"

    # Conversational praise must not open a new cancel while previous op is unresolved.
    praise = router.calendar_action(
        **calendar_tool_args(
            {"action": "cancel", "query": "Сніданок"},
            session_id=SESSION,
            user_utterances=["Молодець"],
        )
    )
    assert praise.status == "ambiguous"
    assert praise.data.get("reason_code") == "ambiguous_blocks_new_mutation"
    assert praise.data.get("op_id") == preview.data["op_id"]
    assert cal.delete_calls == [eid]


def test_google_error_on_delete_forbidden_is_spoken_status(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    eid = seed(cal, when=_soon(1), event_id="breakfast-6")
    preview = router.calendar_action(action="cancel", event_id=eid, session_id=SESSION)
    cal.fail_on["delete"] = GoogleApiError("forbidden", 403, "no")
    done = _confirm(router, preview.data["op_id"])
    assert done.status == "permission_denied"
    assert _should_speak_router_result(done) is True
    assert eid in cal.events


def test_create_and_reschedule_confirmation_still_require_yes(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    frozen = datetime(2026, 9, 29, 12, 0, tzinfo=KYIV)
    router.calendar._now_source = lambda: frozen
    create = router.calendar_action(
        action="create",
        title="Обід",
        date="2026-09-30",
        time="12:00",
        session_id=SESSION,
    )
    assert create.status == "confirmation_required"
    assert cal.create_calls == 0
    done = _confirm(router, create.data["op_id"])
    assert done.status == "success"
    assert cal.create_calls == 1

    eid = seed(cal, when=frozen + timedelta(days=2), summary="Зустріч", event_id="meet-1")
    moved = router.calendar_action(
        action="reschedule",
        event_id=eid,
        new_time="16:00",
        session_id=SESSION,
    )
    assert moved.status == "confirmation_required"
    assert cal.update_calls == []
    assert _confirm(router, moved.data["op_id"]).status == "success"
    assert len(cal.update_calls) == 1
