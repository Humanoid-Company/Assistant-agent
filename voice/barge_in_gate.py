"""Two-stage barge-in: candidate (duck) → confirmed (stop) | rejected (restore)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

import numpy as np

from voice.interrupt_intent import classify_interjection
from voice.local_vad import LocalSpeechDetector, VadTick

logger = logging.getLogger(__name__)

# Echo level smoothing per 30 ms frame (~0.4 s time constant).
_ECHO_ALPHA = 0.08


class BargeInState(str, Enum):
    IDLE = "idle"
    POSSIBLE = "possible_barge_in"
    CONFIRMED = "confirmed_barge_in"


class BargeInAction(str, Enum):
    NONE = "none"
    DUCK = "duck"
    CONFIRM = "confirm"
    REJECT = "reject"


@dataclass
class BargeInDecision:
    action: BargeInAction = BargeInAction.NONE
    state: BargeInState = BargeInState.IDLE
    reason: str = ""
    speech_ms: float = 0.0
    duration_ms: float = 0.0
    # From the transcript classifier: stop | takeover | backchannel | echo | unclear | "".
    intent: str = ""


class BargeInGate:
    """Multi-signal gate so VAD alone does not irreversibly cancel playback."""

    def __init__(
        self,
        *,
        vad: LocalSpeechDetector,
        confirm_ms: int = 250,
        min_speech_ms: int = 180,
        duck_volume: float = 0.3,
        use_energy_gate: bool = True,
        energy_margin: float = 2.2,
        energy_margin_playing: float = 3.0,
        reject_silence_ms: int = 120,
        # Audio-only confirm needs wall-clock age (avoids cough confirming in <100ms).
        min_confirm_age_ms: int = 160,
        # Cough/clap-like bursts are dropped this fast, independent of the longer window.
        burst_reject_ms: int = 300,
        # While the assistant talks, the mic also hears it from the speakers. A candidate must
        # be this many times louder than that echo level to count as the user.
        echo_margin: float = 2.5,
    ) -> None:
        self.vad = vad
        self.confirm_ms = max(100, confirm_ms)
        self.min_speech_ms = max(50, min_speech_ms)
        self.duck_volume = min(1.0, max(0.05, duck_volume))
        self.use_energy_gate = use_energy_gate
        self.energy_margin = energy_margin
        self.energy_margin_playing = energy_margin_playing
        self.reject_silence_ms = max(40, reject_silence_ms)
        self.min_confirm_age_ms = max(80, min_confirm_age_ms)
        self.burst_reject_ms = max(100, burst_reject_ms)
        self.echo_margin = max(1.0, echo_margin)
        # Running mic level while the assistant plays and nobody interrupts = speaker echo.
        self._echo_rms = 0.0

        self.state = BargeInState.IDLE
        self._candidate_started: float | None = None
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._partial_boost = False
        self._strong_hint = False
        self._rms_hist: list[float] = []
        self._zcr_hist: list[float] = []
        self._speech_frames = 0
        self._peak_rms = 0.0
        self._cand_text = ""
        self._last_candidate_at = 0.0

        self.candidate_count = 0
        self.confirmed_count = 0
        self.rejected_count = 0

    def reset(self) -> None:
        self.state = BargeInState.IDLE
        self._candidate_started = None
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._partial_boost = False
        self._strong_hint = False
        self._rms_hist.clear()
        self._zcr_hist.clear()
        self._speech_frames = 0
        self._peak_rms = 0.0
        self._cand_text = ""
        self.vad.reset()

    def feed_mic(
        self,
        pcm: bytes,
        *,
        assistant_or_cue_playing: bool,
    ) -> BargeInDecision:
        """Process mic audio while assistant/cue may be playing."""
        update_ambient = not assistant_or_cue_playing and self.state == BargeInState.IDLE
        tick = self.vad.feed(pcm, update_ambient=update_ambient)

        if not assistant_or_cue_playing and self.state == BargeInState.IDLE:
            self._echo_rms *= 0.8  # playback over -> echo fades out
            return BargeInDecision(state=self.state)

        if self.state == BargeInState.IDLE:
            decision = self._maybe_start_candidate(tick, assistant_or_cue_playing)
            if self.state == BargeInState.IDLE and assistant_or_cue_playing:
                # Learn the echo only from audio that did not open a candidate, so the user's
                # own first syllables never raise the bar against them.
                for rms in tick.frame_rms_list or ((tick.rms,) if tick.rms else ()):
                    self._echo_rms += _ECHO_ALPHA * (rms - self._echo_rms)
            return decision

        if self.state == BargeInState.POSSIBLE:
            return self._update_candidate(tick, assistant_or_cue_playing)

        return BargeInDecision(state=self.state)

    def note_partial_transcript(self, frag: str, *, assistant_recent: str = "") -> BargeInDecision:
        """Transcript evidence for the open candidate: only a stop word or a real takeover
        confirms; listener backchannels and the assistant's own echo reject it."""
        if self.state != BargeInState.POSSIBLE:
            return BargeInDecision(state=self.state)
        if not (frag or "").strip():
            return BargeInDecision(state=self.state)
        self._cand_text += frag
        intent = classify_interjection(self._cand_text, assistant_recent=assistant_recent)
        if intent in ("stop", "takeover"):
            self._strong_hint = True
            decision = self._confirm(reason="transcript")
            decision.intent = intent
            return decision
        if intent in ("backchannel", "echo"):
            decision = self._reject(reason=intent)
            decision.intent = intent
            return decision
        # Real words but not enough yet — speech, not a cough; keep listening.
        self._partial_boost = True
        return BargeInDecision(action=BargeInAction.NONE, state=self.state, intent=intent)

    def had_recent_candidate(self, window_s: float = 1.5) -> bool:
        """Near-field speech (passed the energy gate) started recently. Distinguishes the
        user from far-away room chatter whose words still reach the transcript."""
        return self._last_candidate_at > 0 and time.monotonic() - self._last_candidate_at <= window_s

    def force_confirm(self, *, source: str) -> BargeInDecision:
        """Used by explicit paths that already decided to interrupt."""
        if self.state == BargeInState.CONFIRMED:
            return BargeInDecision(action=BargeInAction.NONE, state=self.state)
        if self.state == BargeInState.IDLE:
            self.candidate_count += 1
            self._candidate_started = time.monotonic()
        return self._confirm(reason=source)

    def _maybe_start_candidate(
        self, tick: VadTick, assistant_playing: bool
    ) -> BargeInDecision:
        if not tick.onset:
            return BargeInDecision(state=self.state)
        too_quiet = self._energy_problem(tick.rms, assistant_playing)
        if too_quiet:
            logger.info(
                "BARGE_IN rejected reason=%s rms=%.0f ambient=%.0f echo=%.0f",
                too_quiet,
                tick.rms,
                tick.ambient_rms,
                self._echo_rms,
            )
            self.rejected_count += 1
            return BargeInDecision(
                action=BargeInAction.NONE,
                state=BargeInState.IDLE,
                reason=too_quiet,
            )
        self.state = BargeInState.POSSIBLE
        self._candidate_started = time.monotonic()
        self._last_candidate_at = self._candidate_started
        # Count only real processed frames — do NOT pad with onset_needed*30
        # (that made coughs confirm in ~78ms wall-clock with speech_ms=180).
        self._speech_ms = float(tick.frames_ms)
        self._silence_ms = 0.0
        self._partial_boost = False
        self._strong_hint = False
        self._rms_hist = list(tick.frame_rms_list) or ([tick.rms] if tick.rms else [])
        self._zcr_hist = list(tick.frame_zcr_list) or ([tick.zcr] if tick.zcr else [])
        self._speech_frames = len(self._rms_hist)
        self._peak_rms = max(self._rms_hist) if self._rms_hist else tick.rms
        self.candidate_count += 1
        logger.info(
            "BARGE_IN candidate source=vad rms=%.0f ambient=%.0f",
            tick.rms,
            tick.ambient_rms,
        )
        logger.info("BARGE_IN ducked")
        return BargeInDecision(
            action=BargeInAction.DUCK,
            state=self.state,
            speech_ms=self._speech_ms,
        )

    def _update_candidate(
        self, tick: VadTick, assistant_playing: bool
    ) -> BargeInDecision:
        now = time.monotonic()
        started = self._candidate_started or now
        age_ms = (now - started) * 1000.0

        energy_ok = self._energy_problem(tick.rms, assistant_playing) is None

        if tick.frame_rms_list:
            self._rms_hist.extend(tick.frame_rms_list)
            self._zcr_hist.extend(tick.frame_zcr_list)
            self._peak_rms = max(self._peak_rms, max(tick.frame_rms_list))
        elif tick.rms:
            self._rms_hist.append(tick.rms)
            self._zcr_hist.append(tick.zcr)
            self._peak_rms = max(self._peak_rms, tick.rms)

        # Cap speech_ms to ~wall clock so buffered multi-frame ticks cannot
        # invent 180ms of speech in 78ms real time (cough false confirm).
        if tick.speaking and tick.speech_frame and energy_ok:
            self._speech_ms = min(age_ms * 1.15, self._speech_ms + tick.frames_ms)
            self._silence_ms = 0.0
            self._speech_frames += max(1, int(tick.frames_ms // 30)) if tick.frames_ms else 1
        elif tick.speaking and tick.speech_frame and not energy_ok:
            self._silence_ms += tick.frames_ms * 0.5
        else:
            self._silence_ms += tick.frames_ms

        burst = self._looks_like_non_speech_burst()
        speech_like = self._looks_like_speech()

        # Transcript decisions happen in note_partial_transcript.
        if self._strong_hint:
            return self._confirm(reason="transcript")

        # Audio-only confirm: need duration + speech-likeness + wall-clock age.
        if (
            self._speech_ms >= self.min_speech_ms
            and energy_ok
            and speech_like
            and not burst
            and age_ms >= self.min_confirm_age_ms
        ):
            return self._confirm(reason="sustained_speech")

        if burst and age_ms >= self.burst_reject_ms and not self._partial_boost:
            return self._reject(reason="non_speech_burst")

        if self._silence_ms >= self.reject_silence_ms and self._speech_ms < self.min_speech_ms:
            return self._reject(reason="short_speech")

        if age_ms >= self.confirm_ms and (burst or not speech_like) and not self._partial_boost:
            return self._reject(reason="non_speech_burst")

        if age_ms >= self.confirm_ms and self._speech_ms < self.min_speech_ms:
            return self._reject(reason="short_speech")

        if age_ms >= self.confirm_ms and not energy_ok:
            return self._reject(reason="low_energy")

        return BargeInDecision(
            action=BargeInAction.NONE,
            state=self.state,
            speech_ms=self._speech_ms,
            duration_ms=age_ms,
        )

    def _energy_problem(self, rms: float, assistant_playing: bool) -> str | None:
        """None if loud enough to be the user; else why not (low_energy / echo_level)."""
        if not self.use_energy_gate:
            return None
        if not self.vad.energy_passes(
            rms,
            margin=self.energy_margin,
            playing_margin=self.energy_margin_playing,
            assistant_playing=assistant_playing,
        ):
            return "low_energy"
        if assistant_playing and rms < self._echo_rms * self.echo_margin:
            return "echo_level"
        return None

    def _looks_like_non_speech_burst(self) -> bool:
        """High-energy simple burst (cough/clap) without speech-like modulation."""
        if len(self._rms_hist) < 2:
            return False
        arr = np.asarray(self._rms_hist, dtype=np.float64)
        median = float(np.median(arr)) or 1.0
        peak_ratio = self._peak_rms / median
        # Very loud relative to ambient baseline baked into hist early frames.
        ambient = max(self.vad.ambient_rms, 1.0)
        peak_vs_ambient = self._peak_rms / ambient
        # Short candidate with extreme peak and little modulation → cough-like.
        if peak_vs_ambient >= 80 and self._speech_ms < self.min_speech_ms * 1.4:
            cv = float(arr.std() / (arr.mean() + 1e-6))
            if cv < 0.55:
                return True
        if peak_ratio >= 3.5 and self._speech_ms <= self.min_speech_ms and len(arr) <= 8:
            return True
        return False

    def _looks_like_speech(self) -> bool:
        """Lightweight speech-likeness: amplitude/ZCR modulation across frames."""
        if self._partial_boost or self._strong_hint:
            return True
        if len(self._rms_hist) < 4:
            return False
        arr = np.asarray(self._rms_hist, dtype=np.float64)
        mean = float(arr.mean()) + 1e-6
        cv = float(arr.std() / mean)
        z = np.asarray(self._zcr_hist, dtype=np.float64) if self._zcr_hist else arr
        z_mean = float(z.mean()) + 1e-6
        z_cv = float(z.std() / z_mean)
        # Speech usually modulates; need several speech frames.
        if self._speech_frames >= 6 and (cv >= 0.12 or z_cv >= 0.15):
            return True
        if self._speech_frames >= 8 and self._speech_ms >= self.min_speech_ms:
            return True
        return False

    def _confirm(self, *, reason: str) -> BargeInDecision:
        now = time.monotonic()
        started = self._candidate_started or now
        duration_ms = (now - started) * 1000.0
        self.state = BargeInState.CONFIRMED
        self.confirmed_count += 1
        logger.info(
            "BARGE_IN confirmed duration_ms=%.0f speech_ms=%.0f reason=%s",
            duration_ms,
            self._speech_ms,
            reason,
        )
        decision = BargeInDecision(
            action=BargeInAction.CONFIRM,
            state=self.state,
            reason=reason,
            speech_ms=self._speech_ms,
            duration_ms=duration_ms,
        )
        self.state = BargeInState.IDLE
        self._candidate_started = None
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._partial_boost = False
        self._strong_hint = False
        self._rms_hist.clear()
        self._zcr_hist.clear()
        self._speech_frames = 0
        self._peak_rms = 0.0
        self._cand_text = ""
        return decision

    def _reject(self, *, reason: str) -> BargeInDecision:
        self.rejected_count += 1
        logger.info("BARGE_IN rejected reason=%s speech_ms=%.0f", reason, self._speech_ms)
        logger.info("BARGE_IN volume_restored")
        decision = BargeInDecision(
            action=BargeInAction.REJECT,
            state=BargeInState.IDLE,
            reason=reason,
            speech_ms=self._speech_ms,
        )
        self.state = BargeInState.IDLE
        self._candidate_started = None
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._partial_boost = False
        self._strong_hint = False
        self._rms_hist.clear()
        self._zcr_hist.clear()
        self._speech_frames = 0
        self._peak_rms = 0.0
        self._cand_text = ""
        self.vad.reset()
        return decision
