"""Local WebRTC VAD + adaptive energy for barge-in gating."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import webrtcvad

# WebRTC VAD accepts 8/16/32 kHz only — Live mic is typically 24 kHz.
_VAD_RATE = 16_000
_FRAME_MS = 30
_FRAME_SAMPLES_16K = _VAD_RATE * _FRAME_MS // 1000  # 480
_FRAME_BYTES_16K = _FRAME_SAMPLES_16K * 2  # 960


def downsample_24k_to_16k(pcm24: bytes) -> bytes:
    """Cheap 3→2 downsample for VAD (int16 mono)."""
    if len(pcm24) < 6:
        return b""
    arr = np.frombuffer(pcm24, dtype=np.int16)
    n = (len(arr) // 3) * 3
    if n == 0:
        return b""
    a = arr[:n].reshape(-1, 3).astype(np.int32)
    out = np.empty(a.shape[0] * 2, dtype=np.int16)
    out[0::2] = a[:, 0]
    out[1::2] = ((a[:, 1] + a[:, 2]) // 2).astype(np.int16)
    return out.tobytes()


def pcm_rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if not len(arr):
        return 0.0
    return float(np.sqrt(np.mean(arr ** 2)))


@dataclass
class VadTick:
    """One mic-chunk observation after VAD frame processing."""

    onset: bool = False
    speaking: bool = False
    speech_frame: bool = False
    rms: float = 0.0
    ambient_rms: float = 0.0
    frames_ms: float = 0.0
    zcr: float = 0.0
    frame_rms_list: tuple[float, ...] = ()
    frame_zcr_list: tuple[float, ...] = ()


def zero_crossing_rate(pcm: bytes) -> float:
    """Zero-crossing rate for int16 mono (speech-likeness cue)."""
    if len(pcm) < 4:
        return 0.0
    arr = np.frombuffer(pcm, dtype=np.int16)
    if len(arr) < 2:
        return 0.0
    signs = np.sign(arr.astype(np.float32))
    signs[signs == 0] = 1.0
    return float(np.mean(np.abs(np.diff(signs)) > 0))


class LocalSpeechDetector:
    """Consecutive-frame VAD onset + slow ambient RMS baseline."""

    def __init__(
        self,
        *,
        sample_rate: int = 24_000,
        aggressiveness: int = 2,
        onset_frames: int = 3,
        rms_fallback: float = 400.0,
        ambient_alpha: float = 0.05,
    ) -> None:
        if aggressiveness not in (0, 1, 2, 3):
            aggressiveness = 2
        self._sample_rate = sample_rate
        self._onset_needed = max(1, onset_frames)
        self._rms_fallback = rms_fallback
        self._ambient_alpha = min(0.5, max(0.01, ambient_alpha))
        self._vad = webrtcvad.Vad(aggressiveness)
        self._buf = b""
        self._onset = 0
        self._speaking = False
        self._ambient_rms = 300.0
        self._last_rms = 0.0
        self._last_zcr = 0.0

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def ambient_rms(self) -> float:
        return self._ambient_rms

    @property
    def last_rms(self) -> float:
        return self._last_rms

    @property
    def last_zcr(self) -> float:
        return self._last_zcr

    def reset(self) -> None:
        self._buf = b""
        self._onset = 0
        self._speaking = False

    def feed(self, pcm: bytes, *, update_ambient: bool = True) -> VadTick:
        """Feed mic PCM. ``onset`` is True once on speech start edge."""
        tick = VadTick(speaking=self._speaking, ambient_rms=self._ambient_rms)
        if not pcm:
            return tick
        if self._sample_rate == 16_000:
            pcm16 = pcm
        else:
            pcm16 = downsample_24k_to_16k(pcm)
        if not pcm16:
            return tick
        self._buf += pcm16
        onset = False
        any_speech = False
        frames_ms = 0.0
        last_rms = self._last_rms
        last_zcr = self._last_zcr
        rms_list: list[float] = []
        zcr_list: list[float] = []
        while len(self._buf) >= _FRAME_BYTES_16K:
            frame = self._buf[:_FRAME_BYTES_16K]
            self._buf = self._buf[_FRAME_BYTES_16K:]
            frames_ms += _FRAME_MS
            rms = pcm_rms(frame)
            zcr = zero_crossing_rate(frame)
            last_rms = rms
            last_zcr = zcr
            rms_list.append(rms)
            zcr_list.append(zcr)
            is_speech = self._is_speech(frame, rms)
            if is_speech:
                any_speech = True
                self._onset += 1
                if not self._speaking and self._onset >= self._onset_needed:
                    self._speaking = True
                    onset = True
            else:
                if update_ambient and not self._speaking:
                    a = self._ambient_alpha
                    self._ambient_rms = (1.0 - a) * self._ambient_rms + a * rms
                self._onset = 0
                self._speaking = False
        self._last_rms = last_rms
        self._last_zcr = last_zcr
        tick.onset = onset
        tick.speaking = self._speaking
        tick.speech_frame = any_speech and self._speaking
        tick.rms = last_rms
        tick.zcr = last_zcr
        tick.ambient_rms = self._ambient_rms
        tick.frames_ms = frames_ms
        tick.frame_rms_list = tuple(rms_list)
        tick.frame_zcr_list = tuple(zcr_list)
        return tick

    def energy_passes(
        self,
        rms: float | None = None,
        *,
        margin: float = 2.2,
        playing_margin: float | None = None,
        assistant_playing: bool = False,
    ) -> bool:
        """True if mic energy is above adaptive ambient * margin."""
        level = self._last_rms if rms is None else rms
        m = playing_margin if (assistant_playing and playing_margin is not None) else margin
        floor = max(80.0, self._ambient_rms * m)
        return level >= floor

    def _is_speech(self, frame_16k: bytes, rms: float) -> bool:
        try:
            return self._vad.is_speech(frame_16k, _VAD_RATE)
        except Exception:
            return rms > self._rms_fallback
