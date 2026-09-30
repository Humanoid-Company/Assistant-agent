"""Multi-turn calendar create: slot filling, title grounding, confirmation speech."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from agents.create_draft import title_said_by_user
from assistant import _model_tool_output, _parse_router_reply, calendar_tool_args
from realtime_client import RealtimeConversation
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
FROZEN = datetime(2026, 9, 29, 12, 0, tzinfo=KYIV)
SUB = "sub-alice"
SESSION = "voice-session"
TODAY = "2026-09-29"


def _freeze(router) -> None:
    router.calendar._now_source = lambda: FROZEN


def _create(router, args: dict, utterances: list[str] | None = None, session_id: str = SESSION):
    return router.calendar_action(
        **calendar_tool_args(args, session_id=session_id, user_utterances=utterances)
    )


def test_date_time_then_title_then_confirm_creates_once(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    first = _create(
        router,
        {"action": "create", "title": "Подія", "date": "сьогодні", "time": "20:00"},
        ["Давай сьогодні додамо вечерю на 8 вечора"],
    )
    assert first.status == "needs_more_info"
    assert first.data["reason_code"] == "title_not_from_user"
    assert router.pending.get(SUB) is None

    named = _create(
        router,
        {"action": "create", "title": "Вечеря з дівчиною", "date": TODAY, "time": "20:00"},
        ["Давай сьогодні додамо вечерю на 8 вечора", "Вечеря з дівчиною"],
    )
    assert named.status == "confirmation_required"
    assert "Вечеря з дівчиною" in named.message
    assert "20:00" in named.message
    assert "сьогодні" in named.message
    assert TODAY not in named.message
    assert cal.create_calls == 0

    done = _create(
        router,
        {"action": "confirm", "confirmation": "так", "op_id": named.data["op_id"]},
        ["Так"],
    )
    assert done.status == "success"
    assert cal.create_calls == 1
    assert cal.events[done.data["event_id"]]["summary"] == "Вечеря з дівчиною"


def test_title_inflection_and_yes_confirms_proposed_title(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    assert title_said_by_user("Вечеря з дівчини", ["Вечеря з дівчиною"])

    ask = _create(
        router,
        {"action": "create", "title": "Вечеря з дівчини", "date": TODAY, "time": "20:00"},
        ["сьогодні о 20:00", "Вечеря з дівчиною"],
    )
    assert ask.status == "confirmation_required"
    assert cal.create_calls == 0

    router2, cal2, *_ = build_test_router(tmp_path / "b")
    _freeze(router2)
    draft = router2.calendar._drafts.get_or_create(SUB, SESSION)
    draft.date = TODAY
    draft.time = "20:00"
    draft.proposed_title = "Вечеря з дівчиною"
    router2.calendar._drafts.save(draft)

    yes = _create(
        router2,
        {"action": "create", "title": "Вечеря з дівчиною", "date": TODAY, "time": "20:00"},
        ["сьогодні на 8 вечора", "так"],
    )
    assert yes.status == "confirmation_required"
    assert "Вечеря з дівчиною" in yes.message
    assert cal2.create_calls == 0
    done = _create(
        router2,
        {"action": "confirm", "confirmation": "yes", "op_id": yes.data["op_id"]},
        ["Так"],
    )
    assert done.status == "success"
    assert cal2.create_calls == 1
    assert cal2.events[done.data["event_id"]]["summary"] == "Вечеря з дівчиною"


def test_soft_title_asks_before_inventing(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    soft = _create(
        router,
        {"action": "create", "title": "Вечеря для пари", "date": "сьогодні", "time": "20:00"},
        ["сьогодні на 8 вечора", "вечеря для двох"],
    )
    assert soft.status == "needs_more_info"
    assert soft.data["reason_code"] in ("title_needs_confirm", "title_not_from_user")
    assert cal.create_calls == 0
    assert router.pending.get(SUB) is None


def test_rename_keeps_date_and_time(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    _create(
        router,
        {"action": "create", "title": "X", "date": "сьогодні", "time": "20:00"},
        ["Давай сьогодні додамо вечерю на 8 вечора"],
    )
    renamed = _create(
        router,
        {"action": "create", "title": "Вечеря вдома", "date": "2026-10-01", "time": "18:00"},
        ["Давай сьогодні додамо вечерю на 8 вечора", "Вечеря вдома"],
    )
    assert renamed.status == "confirmation_required"
    assert (renamed.data or {}).get("reason_code") != "schedule_not_from_user"
    assert "сьогодні" in renamed.message
    assert "20:00" in renamed.message
    assert "18:00" not in renamed.message
    assert "2026-10-01" not in renamed.message
    assert TODAY not in renamed.message
    assert cal.create_calls == 0


def test_thanks_is_not_a_title(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    invented = _create(
        router,
        {"action": "create", "title": "День народження", "date": "сьогодні", "time": "20:00"},
        ["сьогодні на 8 вечора", "Дякую"],
    )
    assert invented.status == "needs_more_info"
    assert invented.data["reason_code"] == "title_not_from_user"
    assert router.pending.get(SUB) is None
    assert cal.create_calls == 0

    hello = _create(
        router,
        {"action": "create", "title": "Зустріч", "date": TODAY, "time": "20:00"},
        ["сьогодні на 8 вечора", "Алло"],
    )
    assert hello.status == "needs_more_info"
    assert hello.data["reason_code"] == "title_not_from_user"
    assert cal.create_calls == 0


def test_confirmation_required_spoken_while_response_in_flight():
    """Tool JSON is delivered and say() speaks even when another response is in flight."""
    spoken: list[str] = []
    sent: list[dict] = []

    conv = RealtimeConversation(tools=[], on_tool_call=lambda *a: None)
    conv._send = sent.append  # type: ignore[method-assign]
    conv.say = lambda text: spoken.append(text)  # type: ignore[method-assign]
    conv._response_done.clear()

    from agents.types import AgentResult

    result = AgentResult(
        "confirmation_required",
        "Створити подію «Вечеря» на сьогодні о 20:00. Підтвердити?",
        {"op_id": "op-1", "kind": "calendar_create"},
    )
    output = _model_tool_output(result)
    reply, awaiting = _parse_router_reply(result)
    assert awaiting is True
    assert "Підтвердити" in reply

    # Mirror assistant._run_router_tool confirmation branch.
    conv.submit_deferred_tool_result(call_id="c1", output=output, trigger_followup=False, allow_tool_calls=False)
    conv.say(result.message)

    assert any(item.get("type") == "conversation.item.create" for item in sent)
    assert spoken == [result.message]
    payload = json.loads(output)
    assert payload["op_id"] == "op-1"
    assert payload["status"] == "confirmation_required"


def test_no_does_not_create(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = _create(
        router,
        {"action": "create", "title": "Вечеря", "date": TODAY, "time": "20:00"},
        ["Вечеря сьогодні о 20:00"],
    )
    rejected = _create(
        router,
        {"action": "confirm", "confirmation": "ні", "op_id": preview.data["op_id"]},
        ["Ні"],
    )
    assert rejected.status == "success"
    assert cal.create_calls == 0
    assert router.pending.get(SUB) is None


def test_double_yes_no_duplicate(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    _freeze(router)
    preview = _create(
        router,
        {"action": "create", "title": "Вечеря", "date": TODAY, "time": "20:00"},
        ["Вечеря сьогодні о 20:00"],
    )
    first = _create(router, {"action": "confirm", "confirmation": "так", "op_id": preview.data["op_id"]})
    second = _create(router, {"action": "confirm", "confirmation": "так", "op_id": preview.data["op_id"]})
    assert first.status == "success"
    assert second.status == "success"
    assert cal.create_calls == 1


def test_draft_not_shared_across_accounts(tmp_path):
    router, cal, _mail, oauth, accounts = build_test_router(tmp_path)
    _freeze(router)
    _create(
        router,
        {"action": "create", "title": "X", "date": "сьогодні", "time": "20:00"},
        ["сьогодні на 8 вечора"],
    )
    assert router.calendar._drafts.get(SUB, SESSION) is not None

    router.disconnect_google()
    assert router.calendar._drafts.get(SUB, SESSION) is None

    from auth.google_oauth import GoogleIdentity
    from tests.helpers_google import FULL_SCOPES, _fake_creds

    bob = GoogleIdentity(sub="sub-bob", email="bob@example.com", name="Bob")
    oauth._identities[bob.sub] = bob
    oauth._next_identity = bob
    accounts._store.save_record(bob.sub, _fake_creds(scopes=FULL_SCOPES).to_json(), FULL_SCOPES)
    accounts._profiles[bob.sub] = {"email": bob.email, "name": bob.name}
    accounts._active_sub = bob.sub
    accounts._session_touch()
    accounts._save_state()

    bob_try = router.calendar_action(
        **calendar_tool_args(
            {"action": "create", "title": "Вечеря", "date": TODAY, "time": "20:00"},
            session_id=SESSION,
            user_utterances=["Вечеря"],
        )
    )
    assert bob_try.status == "needs_more_info"
    assert bob_try.data["reason_code"] == "schedule_not_from_user"
    assert cal.create_calls == 0
    assert router.calendar._drafts.get(SUB, SESSION) is None
