"""Speculative reply: the realtime loop starts the model's reply at end-of-speech, before the
input transcript arrives, and takes the turn back with cancel_reply() when the transcript turns
out to be a local robot command. Nothing from the cancelled reply may reach the user: no audio,
no tool call (a physical action must never run twice), no assistant turn in history.
"""
from __future__ import annotations

from types import SimpleNamespace

from realtime_client import RealtimeConversation


def _make_conversation(tool_calls: list | None = None) -> RealtimeConversation:
    calls = tool_calls if tool_calls is not None else []
    conv = RealtimeConversation(
        tools=[], on_tool_call=lambda name, args, call_id: calls.append(name) or "ok"
    )
    conv.sent = []
    conv._send = conv.sent.append  # type: ignore[method-assign]
    conv.fed = []
    conv.player.feed = conv.fed.append  # type: ignore[method-assign]
    return conv


def _created(response_id: str):
    return SimpleNamespace(type="response.created", response=SimpleNamespace(id=response_id))


def _done(response_id: str, status: str = "completed"):
    return SimpleNamespace(type="response.done", response=SimpleNamespace(id=response_id, status=status))


def _audio(response_id: str):
    return SimpleNamespace(type="response.output_audio.delta", response_id=response_id, delta="AAAA")


def _tool_call(response_id: str, name: str = "control_robot"):
    return SimpleNamespace(
        type="response.function_call_arguments.done",
        response_id=response_id,
        call_id="call-1",
        name=name,
        arguments='{"action": "stop"}',
    )


def test_cancel_after_created_sends_cancel_and_drops_audio_and_tool_calls():
    calls: list = []
    conv = _make_conversation(calls)
    conv.create_response()
    conv._handle_event(_created("resp-1"))

    conv.cancel_reply()

    assert {"type": "response.cancel", "response_id": "resp-1"} in conv.sent
    conv._handle_event(_audio("resp-1"))
    conv._handle_event(_tool_call("resp-1"))
    assert conv.fed == []
    assert calls == []


def test_cancel_before_created_cancels_it_as_soon_as_it_is_created():
    conv = _make_conversation()
    conv.create_response()

    conv.cancel_reply()
    assert not any(p.get("type") == "response.cancel" for p in conv.sent)

    conv._handle_event(_created("resp-2"))
    assert {"type": "response.cancel", "response_id": "resp-2"} in conv.sent


def test_cancel_while_deferred_just_drops_the_queued_request():
    conv = _make_conversation()
    conv._response_done.clear()  # another response in flight → ours is queued
    conv.create_response()
    assert conv._deferred_response_requests == [None]

    conv.cancel_reply()

    assert conv._deferred_response_requests == []
    assert conv.sent == []


def test_cancelled_reply_is_not_recorded_as_an_assistant_turn():
    conv = _make_conversation()
    conv.create_response()
    conv._handle_event(_created("resp-3"))
    conv.cancel_reply()
    conv._handle_event(
        SimpleNamespace(type="response.output_audio_transcript.done", transcript="Зараз розкажу…")
    )
    conv._handle_event(_done("resp-3", status="cancelled"))

    assert conv.get_turns() == []
    assert conv._response_done.is_set()


def test_uncancelled_reply_still_plays_and_runs_tools():
    calls: list = []
    conv = _make_conversation(calls)
    conv.create_response()
    conv._handle_event(_created("resp-4"))
    conv._handle_event(_audio("resp-4"))
    conv._handle_event(_tool_call("resp-4"))

    assert len(conv.fed) == 1
    assert calls == ["control_robot"]


def test_stale_cancel_window_never_cancels_a_later_unrelated_reply():
    conv = _make_conversation()
    conv.create_response()
    conv.cancel_reply()
    conv._cancel_on_created_until = 0.0  # window expired (e.g. response.created was lost)

    conv._handle_event(_created("resp-5"))

    assert not any(p.get("type") == "response.cancel" for p in conv.sent)
