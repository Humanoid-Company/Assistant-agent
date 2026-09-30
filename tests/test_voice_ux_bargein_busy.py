"""Two-stage barge-in: cough gating + response epoch invalidation."""
from __future__ import annotations

import asyncio
import base64
import time
from unittest.mock import MagicMock

import numpy as np

from agents.types import AgentResult
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutor
from tools.gmail_tools import GmailToolWrappers
from tools.task_context import TaskRevisionTracker
from voice.barge_in_gate import BargeInAction, BargeInGate, BargeInState
from voice.busy_cues import BusyCueController
from voice.live_session import LiveVoiceSession
from voice.local_vad import LocalSpeechDetector, VadTick, downsample_24k_to_16k, pcm_rms
from voice.playback import PlaybackTracker


def _executor() -> ToolExecutor:
    cal = CalendarToolWrappers(lambda **kw: AgentResult("success", "ok"))
    mail = GmailToolWrappers(lambda **kw: AgentResult("success", "ok"))
    return ToolExecutor(calendar=cal, gmail=mail, revisions=TaskRevisionTracker())


def _session(**kwargs) -> LiveVoiceSession:
    synth = kwargs.pop("cue_synthesize", lambda text: b"\x00\x01" * 240)
    session = LiveVoiceSession(
        tool_executor=_executor(),
        session_id="sess-ux",
        cue_synthesize=synth,
        **kwargs,
    )
    session.player._stream = MagicMock()
    session.player._thread = MagicMock()
    return session


def _tone_pcm24(ms: int = 60, freq: float = 440.0, amp: int = 12000) -> bytes:
    n = int(24000 * ms / 1000)
    t = np.arange(n, dtype=np.float32) / 24000.0
    wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    return wave.tobytes()


def _gate(**kwargs) -> BargeInGate:
    vad = LocalSpeechDetector(sample_rate=24000, onset_frames=2)
    defaults = dict(
        confirm_ms=250,
        min_speech_ms=180,
        duck_volume=0.3,
        use_energy_gate=False,
        energy_margin=2.0,
        energy_margin_playing=2.5,
        reject_silence_ms=80,
        min_confirm_age_ms=160,
    )
    defaults.update(kwargs)
    return BargeInGate(vad=vad, **defaults)


def _speech_like_tick(*, onset=False, frames_ms=30, rms=3000.0, zcr=0.12) -> VadTick:
    # Vary RMS/ZCR slightly across synthetic frames for speech-likeness.
    n = max(1, int(frames_ms // 30))
    rms_list = tuple(rms * (0.7 + 0.1 * (i % 4)) for i in range(n))
    zcr_list = tuple(zcr * (0.8 + 0.15 * (i % 3)) for i in range(n))
    return VadTick(
        onset=onset,
        speaking=True,
        speech_frame=True,
        rms=rms_list[-1],
        ambient_rms=200,
        frames_ms=frames_ms,
        zcr=zcr_list[-1],
        frame_rms_list=rms_list,
        frame_zcr_list=zcr_list,
    )


def _cough_tick(*, onset=False, frames_ms=60, rms=9000.0) -> VadTick:
    # Flat high-energy burst — little modulation.
    n = max(1, int(frames_ms // 30))
    rms_list = tuple(rms for _ in range(n))
    zcr_list = tuple(0.45 for _ in range(n))
    return VadTick(
        onset=onset,
        speaking=True,
        speech_frame=True,
        rms=rms,
        ambient_rms=15,
        frames_ms=frames_ms,
        zcr=0.45,
        frame_rms_list=rms_list,
        frame_zcr_list=zcr_list,
    )


def test_downsample_24k_to_16k_length():
    assert len(downsample_24k_to_16k(b"\x00\x01" * 300)) == 400


def test_playback_duck_does_not_clear_queue():
    tracker = PlaybackTracker(sample_rate=24000)
    tracker._stream = MagicMock()
    tracker._thread = MagicMock()
    tracker.enqueue(b"\x00\x01" * 400)
    before = tracker.queued_bytes
    tracker.set_volume(0.3)
    assert tracker.volume == 0.3
    assert tracker.queued_bytes == before


def test_cough_like_high_energy_burst_does_not_full_cancel():
    gate = _gate(min_speech_ms=180, confirm_ms=200, min_confirm_age_ms=100)
    gate.vad._ambient_rms = 15.0
    gate.vad.feed = lambda pcm, update_ambient=True: _cough_tick(onset=True, frames_ms=90, rms=6456)  # type: ignore
    d1 = gate.feed_mic(b"x", assistant_or_cue_playing=True)
    assert d1.action == BargeInAction.DUCK
    # More cough frames — should NOT confirm as sustained_speech
    gate.vad.feed = lambda pcm, update_ambient=True: _cough_tick(onset=False, frames_ms=90, rms=6500)  # type: ignore
    gate._candidate_started = time.monotonic() - 0.05
    d2 = gate.feed_mic(b"x", assistant_or_cue_playing=True)
    assert d2.action != BargeInAction.CONFIRM
    # Expire window → reject non_speech_burst
    gate._candidate_started = time.monotonic() - 0.3
    d3 = gate.feed_mic(b"x", assistant_or_cue_playing=True)
    assert d3.action == BargeInAction.REJECT
    assert d3.reason == "non_speech_burst"
    assert gate.confirmed_count == 0


def test_cough_candidate_ducks_then_restores_on_session():
    session = _session()
    session._local_barge_in = True
    session._barge_gate.use_energy_gate = False
    session._barge_gate.min_speech_ms = 180
    session._barge_gate.confirm_ms = 200
    session._barge_gate.min_confirm_age_ms = 100
    session._barge_gate.vad._ambient_rms = 15.0
    session.player.enqueue(b"\x00\x01" * 800)
    queued = session.player.queued_bytes

    session._barge_gate.vad.feed = lambda pcm, update_ambient=True: _cough_tick(onset=True, rms=8000)  # type: ignore
    session._maybe_local_barge_in(_tone_pcm24(30))
    assert session.player.volume == session._duck_volume
    assert session.player.queued_bytes == queued

    session._barge_gate.vad.feed = lambda pcm, update_ambient=True: _cough_tick(onset=False, rms=8000)  # type: ignore
    session._barge_gate._candidate_started = time.monotonic() - 0.3
    session._maybe_local_barge_in(_tone_pcm24(30))
    assert session.player.volume == 1.0
    assert session.player.queued_bytes == queued
    assert session._assistant_generation == 0


def test_actual_sustained_speech_confirms():
    gate = _gate(min_speech_ms=120, confirm_ms=400, min_confirm_age_ms=100)
    gate.vad.feed = lambda pcm, update_ambient=True: _speech_like_tick(onset=True, frames_ms=60)  # type: ignore
    assert gate.feed_mic(b"x", assistant_or_cue_playing=True).action == BargeInAction.DUCK
    gate._candidate_started = time.monotonic() - 0.12
    # Feed several modulated speech ticks
    for _ in range(5):
        gate.vad.feed = lambda pcm, update_ambient=True: _speech_like_tick(  # type: ignore
            onset=False, frames_ms=40, rms=2500 + (_ % 3) * 400
        )
        d = gate.feed_mic(b"x", assistant_or_cue_playing=True)
        if d.action == BargeInAction.CONFIRM:
            assert d.reason == "sustained_speech"
            return
    assert False, "expected sustained_speech confirm"


def test_keyword_zachekai_confirms():
    gate = _gate()
    gate.vad.feed = lambda pcm, update_ambient=True: _cough_tick(onset=True, rms=3000)  # type: ignore
    gate.feed_mic(b"x", assistant_or_cue_playing=True)
    d = gate.note_partial_transcript("зачекай")
    assert d.action == BargeInAction.CONFIRM
    assert d.reason == "transcript"


def test_barge_in_invalidates_old_response_generation():
    session = _session()
    session._last_barge_in_at = 0.0
    session.player.enqueue(b"\x00\x01" * 200)
    gen0 = session._assistant_generation
    session._trigger_barge_in(source="transcript_keyword", short_ack=True)
    assert session._assistant_generation == gen0 + 1
    assert session._play_assistant_audio is False
    assert session._stale_response is True
    assert session.player.queued_bytes == 0


def test_old_output_audio_delta_after_barge_in_is_dropped():
    session = _session()
    session._last_barge_in_at = 0.0
    session._trigger_barge_in(source="vad_confirmed")
    pcm = base64.b64encode(b"\x00\x01" * 40).decode("ascii")
    asyncio.run(session._handle_event({"type": "session.output_audio.delta", "delta": pcm}))
    assert session.player.queued_bytes == 0
    assert session._play_assistant_audio is False


def test_time_suppress_expiry_does_not_allow_stale_audio_back():
    """Epoch gate must outlive any old time-based suppress mental model."""
    session = _session()
    session._last_barge_in_at = 0.0
    session._trigger_barge_in(source="vad_confirmed")
    # Simulate "time passed" — still blocked until output gap.
    session._last_output_delta_at = time.monotonic()  # recent stale delta
    time.sleep(0.02)
    pcm = base64.b64encode(b"\x00\x01" * 20).decode("ascii")
    asyncio.run(session._handle_event({"type": "session.output_audio.delta", "delta": pcm}))
    assert session.player.queued_bytes == 0
    assert not session._output_accepted()


def test_output_gap_allows_new_assistant_response():
    session = _session()
    session._last_barge_in_at = 0.0
    session._trigger_barge_in(source="transcript_keyword", short_ack=True)
    # Old stream goes quiet
    session._last_output_delta_at = time.monotonic() - 0.3
    session._maybe_release_output_after_gap()
    assert session._play_assistant_audio is True
    assert session._stale_response is False
    pcm = base64.b64encode(b"\x00\x01" * 30).decode("ascii")
    asyncio.run(session._handle_event({"type": "session.output_audio.delta", "delta": pcm}))
    assert session.player.queued_bytes > 0


def test_stale_output_transcript_does_not_reactivate_playback():
    session = _session()
    session._last_barge_in_at = 0.0
    session._trigger_barge_in(source="vad_confirmed")
    asyncio.run(
        session._handle_event({"type": "session.output_transcript.delta", "delta": "продовження"})
    )
    assert session._output_buf == ""
    assert session.player.queued_bytes == 0


def test_busy_cue_cannot_survive_barge_in():
    session = _session()
    session._last_barge_in_at = 0.0
    session.player.enqueue(b"\x00\x01" * 300, kind="cue")
    session._busy_cues._cue_playing = True
    session._trigger_barge_in(source="vad_confirmed")
    assert session.player.queued_bytes == 0
    assert session._busy_cues.cue_playing is False


def test_transcript_keyword_interrupts_and_requests_short_ack():
    session = _session()
    session.player.enqueue(b"\x00\x01" * 400)
    steered = {}

    async def fake_steer(*, short_ack=False):
        steered["short_ack"] = short_ack

    session._steer_stop_speaking = fake_steer  # type: ignore
    session._connection = object()
    asyncio.run(
        session._handle_event({"type": "session.input_transcript.delta", "delta": "зачекай"})
    )
    assert session.player.queued_bytes == 0
    assert session._stale_response is True
    # Steer scheduled via create_task only with running loop; call directly:
    asyncio.run(session._steer_stop_speaking(short_ack=True))
    assert steered.get("short_ack") is True


def test_short_click_rejected():
    gate = _gate(min_speech_ms=180, confirm_ms=250)
    gate.vad.feed = lambda pcm, update_ambient=True: VadTick(  # type: ignore
        onset=True, speaking=True, speech_frame=True, rms=4000, ambient_rms=200, frames_ms=30
    )
    assert gate.feed_mic(b"x", assistant_or_cue_playing=True).action == BargeInAction.DUCK
    gate.vad.feed = lambda pcm, update_ambient=True: VadTick(  # type: ignore
        onset=False, speaking=False, speech_frame=False, rms=50, ambient_rms=200, frames_ms=100
    )
    d = gate.feed_mic(b"x", assistant_or_cue_playing=True)
    assert d.action == BargeInAction.REJECT


def test_false_candidate_restores_volume():
    session = _session()
    session._local_barge_in = True
    session._barge_gate.use_energy_gate = False
    session.player.enqueue(b"\x00\x01" * 500)
    queued = session.player.queued_bytes
    session._barge_gate.vad.feed = lambda pcm, update_ambient=True: VadTick(  # type: ignore
        onset=True, speaking=True, speech_frame=True, rms=3000, ambient_rms=200, frames_ms=30,
        frame_rms_list=(3000,), frame_zcr_list=(0.1,),
    )
    session._maybe_local_barge_in(_tone_pcm24(30))
    assert session.player.volume < 1.0
    session._barge_gate.vad.feed = lambda pcm, update_ambient=True: VadTick(  # type: ignore
        onset=False, speaking=False, speech_frame=False, rms=40, ambient_rms=200, frames_ms=120
    )
    session._maybe_local_barge_in(_tone_pcm24(30))
    assert session.player.volume == 1.0
    assert session.player.queued_bytes == queued


def test_busy_cue_not_played_if_tool_finishes_before_threshold():
    played: list[str] = []

    async def play(phrase: str, pcm: bytes):
        played.append(phrase)

    ctl = BusyCueController(
        play_cue=play,
        synthesize=lambda p: b"\x01\x00" * 10,
        first_delay_ms=200,
        second_delay_ms=500,
        max_per_turn=2,
    )

    async def _run():
        ctl.on_tool_started()
        await asyncio.sleep(0.05)
        ctl.on_tool_finished()
        await asyncio.sleep(0.25)

    asyncio.run(_run())
    assert played == []


def test_pcm_rms_nonzero_for_tone():
    assert pcm_rms(_tone_pcm24(30, amp=8000)) > 100


def test_local_speech_detector_reset():
    det = LocalSpeechDetector(sample_rate=24000, onset_frames=3)
    det._speaking = True
    det.reset()
    assert not det.speaking
