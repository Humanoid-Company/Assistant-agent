"""
Speech-to-text via Google STT — used only while SLEEPING (wake-phrase
detection). The live conversation after wake-up goes through the persistent
Realtime API session in `realtime_client.py`, which does its own transcription.

Recording via sounddevice persistent stream (opened once per session).
Transcription via Google Speech Recognition — fast, ~0.3–0.8 s.

listen() returns (text, raw_pcm, emotion) — emotion is always "" here.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import sounddevice as sd
import speech_recognition as sr
import webrtcvad

from config import (
    CHANNELS,
    CHUNK_SIZE,
    LANGUAGE_BCP47,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    STT_ENERGY_THRESHOLD,
    STT_NON_SPEAKING_DURATION,
    STT_PAUSE_THRESHOLD,
    STT_PHRASE_LIMIT,
    STT_TIMEOUT,
)

logger = logging.getLogger(__name__)

# WebRTC VAD — filters out non-speech sounds (music, steps, street noise)
# Aggressiveness: 0 (lenient) – 3 (strict). 2 = good balance for noisy environments.
_VAD_AGGRESSIVENESS = 2
_VAD_FRAME_BYTES    = 960   # 30 ms × 16 000 Hz × 2 bytes/sample (int16)
_VAD_ONSET_FRAMES   = 2     # consecutive VAD-positive chunks needed to start recording


def _rms(chunk: bytes) -> float:
    arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(arr ** 2))) if len(arr) else 0.0


def _normalise(raw_pcm: bytes, target_rms: float = 3000.0) -> bytes:
    """Amplify quiet audio up to target RMS (max 10× gain)."""
    arr = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(arr ** 2)))
    if rms < 50:
        return raw_pcm
    gain = min(target_rms / rms, 10.0)
    arr = np.clip(arr * gain, -32000, 32000)
    return arr.astype(np.int16).tobytes()


class SpeechToText:
    """Microphone listener with Google STT transcription."""

    def __init__(self) -> None:
        self._recognizer = sr.Recognizer()
        self._energy_threshold = STT_ENERGY_THRESHOLD
        self._vad = webrtcvad.Vad(_VAD_AGGRESSIVENESS)
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        self._stream.start()
        self._calibrate()

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    # ── Public API ────────────────────────────────────────────────────────────

    def read_chunk(self) -> bytes:
        """Read one raw PCM chunk (16 kHz, int16) from the persistent mic stream.

        Used by RealtimeConversation's feeder thread once awake — the same
        stream this class uses for wake-phrase detection while SLEEPING.
        """
        data, _ = self._stream.read(CHUNK_SIZE)
        return bytes(data)

    def listen(self) -> tuple[str | None, bytes | None, str]:
        """
        Block until a speech phrase is captured.
        Returns (transcription, raw_pcm, emotion).
        emotion is always "" — voice emotion detection is a planned feature.
        """
        try:
            raw_pcm = self._record(timeout=STT_TIMEOUT, phrase_time_limit=STT_PHRASE_LIMIT)
            if raw_pcm is None:
                return None, None, ""
            text = self._transcribe(raw_pcm)
            return text, raw_pcm, ""
        except Exception as exc:
            logger.error("listen() error: %s", exc)
            return None, None, ""

    # ── Private ───────────────────────────────────────────────────────────────

    def _calibrate(self) -> None:
        logger.info("Calibrating microphone…")
        frames: list[bytes] = []
        end_time = time.monotonic() + 1.5
        try:
            while time.monotonic() < end_time:
                data, _ = self._stream.read(CHUNK_SIZE)
                frames.append(bytes(data))
        except Exception as exc:
            logger.warning("Calibration error: %s — using default threshold", exc)
        if frames:
            ambient = sum(_rms(f) for f in frames) / len(frames)
            self._energy_threshold = max(STT_ENERGY_THRESHOLD, ambient * 1.5)
        logger.info("Calibration done — threshold: %.0f", self._energy_threshold)

    def _is_speech(self, chunk: bytes) -> bool:
        """True if chunk contains human speech (WebRTC VAD).
        Falls back to RMS threshold if chunk is too short for VAD."""
        if len(chunk) < _VAD_FRAME_BYTES:
            return _rms(chunk) > self._energy_threshold
        try:
            return self._vad.is_speech(chunk[:_VAD_FRAME_BYTES], SAMPLE_RATE)
        except Exception:
            return _rms(chunk) > self._energy_threshold

    def _record(self, timeout: float, phrase_time_limit: float) -> bytes | None:
        pre_frames = max(1, int(STT_NON_SPEAKING_DURATION * SAMPLE_RATE / CHUNK_SIZE))
        max_silence = max(1, int(STT_PAUSE_THRESHOLD * SAMPLE_RATE / CHUNK_SIZE))

        # Phase 1: wait for confirmed speech onset (VAD must agree for N frames)
        pre_buffer: list[bytes] = []
        onset_count = 0
        start = time.monotonic()
        while True:
            if time.monotonic() - start > timeout:
                return None
            data, _ = self._stream.read(CHUNK_SIZE)
            chunk = bytes(data)
            pre_buffer.append(chunk)
            if len(pre_buffer) > pre_frames:
                pre_buffer.pop(0)
            if self._is_speech(chunk):
                onset_count += 1
                if onset_count >= _VAD_ONSET_FRAMES:
                    break
            else:
                onset_count = 0

        # Phase 2: record until VAD-confirmed silence
        recorded = list(pre_buffer)
        phrase_start = time.monotonic()
        silence_count = 0
        while True:
            if time.monotonic() - phrase_start > phrase_time_limit:
                break
            data, _ = self._stream.read(CHUNK_SIZE)
            chunk = bytes(data)
            recorded.append(chunk)
            if not self._is_speech(chunk):
                silence_count += 1
                if silence_count >= max_silence:
                    break
            else:
                silence_count = 0

        return b"".join(recorded)

    def _transcribe(self, raw_pcm: bytes) -> str | None:
        normalised = _normalise(raw_pcm)
        try:
            audio = sr.AudioData(normalised, SAMPLE_RATE, SAMPLE_WIDTH)
            text = self._recognizer.recognize_google(audio, language=LANGUAGE_BCP47)
            text = text.lower().strip()
            logger.info("Recognized: %r", text)
            return text
        except sr.UnknownValueError:
            logger.debug("Google STT: could not understand audio")
            return None
        except Exception as exc:
            logger.warning("Google STT error: %s", exc)
            return None
