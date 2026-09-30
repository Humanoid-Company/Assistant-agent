"""Microphone helpers for wake (16 kHz) vs Live (native 24 kHz when possible)."""
from __future__ import annotations

import logging
from typing import Callable, Optional

import sounddevice as sd

from config import CHANNELS, CHUNK_SIZE, SAMPLE_RATE
from realtime_client import resample_16k_to_24k

logger = logging.getLogger(__name__)

LIVE_RATE = 24_000


class LiveMicCapture:
    """Awake-path mic capture at 24 kHz when the device allows it.

    Falls back to the shared 16 kHz reader + resample if opening 24 kHz fails.
    Sleep/wake STT keeps using SpeechToText at 16 kHz.
    """

    def __init__(self, fallback_read_chunk: Callable[[], bytes]) -> None:
        self._fallback = fallback_read_chunk
        self._stream: Optional[sd.RawInputStream] = None
        self._native_24k = False

    def open(self) -> None:
        try:
            stream = sd.RawInputStream(
                samplerate=LIVE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=max(CHUNK_SIZE, 768),
            )
            stream.start()
            self._stream = stream
            self._native_24k = True
            logger.info("live.mic native_24k=true")
        except Exception as exc:
            self._stream = None
            self._native_24k = False
            logger.warning(
                "live.mic native_24k unavailable (%s) — using 16k→24k resample fallback",
                type(exc).__name__,
            )

    def read_chunk(self) -> bytes:
        if self._stream is not None and self._native_24k:
            data, _ = self._stream.read(max(CHUNK_SIZE, 768))
            return bytes(data)
        return resample_16k_to_24k(self._fallback())

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._native_24k = False

    @property
    def using_native_24k(self) -> bool:
        return self._native_24k
