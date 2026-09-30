"""Generic local playback tracker — not coupled to Realtime event names."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

import sounddevice as sd

logger = logging.getLogger(__name__)


class PlaybackTracker:
    """Tracks queued PCM playback independently of backend completion."""

    def __init__(self, *, sample_rate: int = 24_000, channels: int = 1) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._stream: Optional[sd.RawOutputStream] = None
        self._thread: Optional[threading.Thread] = None
        self._bytes_queued = 0
        self._bytes_played = 0
        self._lock = threading.Lock()
        self._playing = False
        self._empty = threading.Event()
        self._empty.set()

    def start(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.RawOutputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="int16",
        )
        self._stream.start()
        self._thread = threading.Thread(target=self._write_loop, daemon=True, name="playback-tracker")
        self._thread.start()

    def enqueue(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            self._bytes_queued += len(pcm)
            self._empty.clear()
        self._queue.put(pcm)

    def interrupt(self) -> None:
        """Drain not-yet-played audio (barge-in). Does not abort PortAudio mid-write."""
        while True:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            if chunk is None:
                self._queue.put(None)
                break
            with self._lock:
                self._bytes_queued = max(0, self._bytes_queued - len(chunk))
        with self._lock:
            if self._queue.empty() and not self._playing:
                self._empty.set()

    def reset(self) -> None:
        self.interrupt()
        with self._lock:
            self._bytes_queued = 0
            self._bytes_played = 0
            self._empty.set()

    async def wait_until_empty(self, timeout: float | None = None) -> bool:
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._empty.wait(timeout))

    def wait_until_empty_sync(self, timeout: float | None = None) -> bool:
        return self._empty.wait(timeout)

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing or not self._queue.empty()

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return self._bytes_queued

    def close(self) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        with self._lock:
            self._bytes_queued = 0
            self._playing = False
            self._empty.set()

    def _write_loop(self) -> None:
        while True:
            chunk = self._queue.get()
            if chunk is None:
                break
            with self._lock:
                self._playing = True
            try:
                if self._stream is not None:
                    self._stream.write(chunk)
                with self._lock:
                    self._bytes_played += len(chunk)
                    self._bytes_queued = max(0, self._bytes_queued - len(chunk))
            except Exception as exc:
                logger.warning("Playback write error: %s", exc)
            finally:
                with self._lock:
                    self._playing = False
                    if self._queue.empty() and self._bytes_queued == 0:
                        self._empty.set()
