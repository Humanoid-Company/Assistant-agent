"""Tests for the tool_choice-gating mechanism in RealtimeConversation (realtime_client.py).

This covers the one real bug this project has actually hit in practice: the model reading back
a confirmation question (e.g. dispatch_task's "...Підтвердити?") and then, with nothing stopping
it, immediately continuing on its own into a tool call — confirming its own question with no real
user reply in between. The fix threads a `tool_choice` override through create_response() /
submit_deferred_tool_result(); these tests exercise that mechanism directly, with no real
OpenAI Realtime connection (RealtimeConversation.__init__ never opens one — only .connect() does,
which these tests never call).
"""
from __future__ import annotations

from types import SimpleNamespace

from realtime_client import RealtimeConversation


def _make_conversation() -> RealtimeConversation:
    conv = RealtimeConversation(tools=[], on_tool_call=lambda name, args, call_id: None)
    conv.sent = []
    conv._send = conv.sent.append  # type: ignore[method-assign]
    return conv


def _response_done_event(status: str = "completed"):
    return SimpleNamespace(type="response.done", response=SimpleNamespace(status=status))


def test_create_response_when_idle_sends_bare_payload():
    conv = _make_conversation()
    conv.create_response()
    assert conv.sent == [{"type": "response.create"}]


def test_create_response_with_tool_choice_none_is_included_in_the_payload():
    conv = _make_conversation()
    conv.create_response(tool_choice="none")
    assert conv.sent == [{"type": "response.create", "response": {"tool_choice": "none"}}]


def test_create_response_while_a_response_is_in_flight_defers_instead_of_sending():
    conv = _make_conversation()
    conv._response_done.clear()  # simulate a response already in flight
    conv.create_response(tool_choice="none")
    assert conv.sent == []
    assert conv._deferred_response_requests == ["none"]


def test_deferred_followup_fires_with_the_stored_tool_choice_once_idle():
    """The exact mechanism behind the reported bug: a response.done arriving while a
    tool_choice="none" follow-up was deferred must fire that SAME restriction, not silently
    fall back to unrestricted "auto" and let the model call a tool on its own."""
    conv = _make_conversation()
    conv._response_done.clear()
    conv.create_response(tool_choice="none")  # deferred, nothing sent yet
    assert conv.sent == []

    conv._responses_requested = 1  # the in-flight response this was deferred behind
    conv._handle_event(_response_done_event())

    assert conv.sent == [{"type": "response.create", "response": {"tool_choice": "none"}}]
    assert conv._deferred_response_requests == []


def test_two_deferred_requests_both_fire_in_order_not_just_the_last_one():
    """The actual bug this queue replaces a single slot to fix: a SECOND create_response() call
    arriving while the first was still deferred used to silently overwrite it — losing that
    turn's reply outright instead of just delaying it (the real incident: a forced silent-tool
    follow-up collided with dispatch_task's own deferred result, and the dispatch_task reply
    never got spoken). Two deferred calls now both eventually fire, oldest first."""
    conv = _make_conversation()
    conv._response_done.clear()  # a response is already in flight
    conv.create_response(tool_choice="none")  # 1st deferred call
    conv.create_response(tool_choice="auto")  # 2nd deferred call, arrives before the 1st fires
    assert conv.sent == []
    assert conv._deferred_response_requests == ["none", "auto"]

    # The in-flight response completes — the OLDEST deferred call fires, not the newest.
    conv._responses_requested = 1
    conv._handle_event(_response_done_event())
    assert conv.sent == [{"type": "response.create", "response": {"tool_choice": "none"}}]
    assert conv._deferred_response_requests == ["auto"]

    # That new response completes in turn — the 2nd deferred call fires next, nothing lost.
    conv._handle_event(_response_done_event())
    assert conv.sent == [
        {"type": "response.create", "response": {"tool_choice": "none"}},
        {"type": "response.create", "response": {"tool_choice": "auto"}},
    ]
    assert conv._deferred_response_requests == []


def test_deferred_followup_with_no_override_restores_auto_once_idle():
    conv = _make_conversation()
    conv._response_done.clear()
    conv.create_response()  # deferred with no tool_choice override
    conv._responses_requested = 1
    conv._handle_event(_response_done_event())

    assert conv.sent == [{"type": "response.create"}]


def test_submit_deferred_tool_result_blocks_tool_calls_when_disallowed():
    """assistant.py's dispatch_task worker passes allow_tool_calls=False whenever the router's
    reply status is needs_more_info/confirmation_required — i.e. the reply IS a question."""
    conv = _make_conversation()
    conv.submit_deferred_tool_result("call_1", "Підтвердити?", allow_tool_calls=False)
    assert conv.sent[0]["type"] == "conversation.item.create"
    assert conv.sent[1] == {"type": "response.create", "response": {"tool_choice": "none"}}


def test_submit_deferred_tool_result_allows_tool_calls_by_default():
    conv = _make_conversation()
    conv.submit_deferred_tool_result("call_1", "Готово.")
    assert conv.sent[1] == {"type": "response.create"}


def test_a_fresh_genuine_user_turn_never_carries_a_tool_choice_override():
    """assistant.py's own turn-completion call to create_response() takes no arguments — this
    is what lifts a prior "none" restriction back to normal once the user has actually replied."""
    conv = _make_conversation()
    conv.create_response()
    assert conv.sent[-1] == {"type": "response.create"}
    assert "response" not in conv.sent[-1]
