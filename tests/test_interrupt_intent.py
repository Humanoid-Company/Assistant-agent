"""Interrupt only on real intent: stop words or the user taking the turn — not on listener
backchannels, room chatter or the assistant's own voice coming back through the mic."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from agents.types import AgentResult
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutor
from tools.task_context import TaskRevisionTracker
from voice.barge_in_gate import BargeInState
from voice.interrupt_intent import classify_interjection
from voice.live_session import LiveVoiceSession
from voice.local_vad import VadTick


@pytest.mark.parametrize(
    "text, expected",
    [
        ("стоп", "stop"),
        ("Зачекай!", "stop"),
        ("не треба", "stop"),
        ("hold on", "stop"),
        ("угу", "backchannel"),
        ("ага-ага", "backchannel"),
        ("так", "backchannel"),
        ("ну да", "backchannel"),
        ("хаха", "backchannel"),
        ("а котра година", "takeover"),
        ("слухай", "takeover"),
        ("ні-ні, я про інше", "takeover"),
        ("так, а скажи ще", "takeover"),
        ("а", "backchannel"),
        ("котра", "unclear"),
        ("", "unclear"),
    ],
)
def test_classify_interjection(text, expected):
    assert classify_interjection(text) == expected


def test_assistant_echo_is_not_an_interruption():
    said = "Сьогодні у вас зустріч з командою о третій, а ввечері вечеря."
    assert classify_interjection("зустріч з командою", assistant_recent=said) == "echo"
    # The assistant explaining a command must not stop itself through the mic.
    assert classify_interjection("скажіть стоп", assistant_recent="Якщо що — просто скажіть стоп.") == "echo"
    # The user really saying different words is still a takeover.
    assert classify_interjection("а що завтра", assistant_recent=said) == "takeover"


def _session() -> LiveVoiceSession:
    cal = CalendarToolWrappers(lambda **kw: AgentResult("success", "ok"))
    session = LiveVoiceSession(
        tool_executor=ToolExecutor(calendar=cal, revisions=TaskRevisionTracker()),
        session_id="sess-intent",
        cue_synthesize=lambda text: b"\x00\x01" * 240,
    )
    session.player._stream = MagicMock()
    session.player._thread = MagicMock()
    session._barge_gate.use_energy_gate = False
    session.player.enqueue(b"\x00\x01" * 2000)  # assistant is talking
    return session


def _open_candidate(session: LiveVoiceSession) -> None:
    session._barge_gate.vad.feed = lambda pcm, update_ambient=True: VadTick(  # type: ignore
        onset=True, speaking=True, speech_frame=True, rms=3000, ambient_rms=200, frames_ms=30,
        frame_rms_list=(3000,), frame_zcr_list=(0.1,),
    )
    session._maybe_local_barge_in(b"\x00\x01" * 360)
    assert session._barge_gate.state == BargeInState.POSSIBLE
    assert session.player.volume < 1.0


def _hear(session: LiveVoiceSession, text: str) -> None:
    asyncio.run(session._handle_event({"type": "session.input_transcript.delta", "delta": text}))


def _steers(session: LiveVoiceSession) -> list[bool]:
    calls: list[bool] = []
    session._schedule_steer_stop = lambda *, short_ack=False, **_: calls.append(short_ack)  # type: ignore
    return calls


def test_backchannel_keeps_talking_and_restores_volume():
    session = _session()
    _open_candidate(session)
    _hear(session, "угу")
    assert session._assistant_generation == 0
    assert session.player.queued_bytes > 0
    assert session.player.volume == 1.0


def test_echo_of_own_words_keeps_talking():
    session = _session()
    asyncio.run(
        session._handle_event(
            {"type": "session.output_transcript.delta", "delta": "Завтра о десятій у вас стендап з командою."}
        )
    )
    _open_candidate(session)
    _hear(session, "стендап з командою")
    assert session._assistant_generation == 0
    assert session.player.volume == 1.0


def test_user_taking_the_turn_interrupts_without_scripted_ack():
    session = _session()
    steers = _steers(session)
    _open_candidate(session)
    _hear(session, "а що в мене")
    _hear(session, " завтра")  # partials accumulate; the first two words already decide
    assert session._assistant_generation == 1
    assert session.player.queued_bytes == 0
    assert steers == [False]


def test_stop_word_interrupts_with_short_ack():
    session = _session()
    steers = _steers(session)
    _hear(session, "стоп")  # no open candidate: a short word that already ended
    assert session._assistant_generation == 1
    assert steers == [True]


def test_room_chatter_without_near_field_speech_does_not_interrupt():
    session = _session()
    _hear(session, "і тоді він каже, що квитки вже закінчились")
    assert session._assistant_generation == 0
    assert session.player.queued_bytes > 0


def test_late_stop_word_after_a_short_candidate_interrupts():
    session = _session()
    _open_candidate(session)
    session._barge_gate._reject(reason="short_speech")  # candidate ended before ASR caught up
    session.player.set_volume(1.0)
    _hear(session, "почекай")
    assert session._assistant_generation == 1


def test_idle_takeover_needs_a_recent_candidate():
    session = _session()
    session._barge_gate._last_candidate_at = time.monotonic() - 10  # long ago
    _hear(session, "а скільки коштує квиток")
    assert session._assistant_generation == 0
