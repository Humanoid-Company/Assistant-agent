"""Generic local playback tracker — not coupled to Realtime event names."""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

# Queue items: (pcm_bytes, generation, kind) where kind is "assistant" | "cue"
_QueueItem = tuple[bytes, int, str]


class PlaybackTracker:
    """Tracks queued PCM playback independently of backend completion.

    ``interrupt()`` drains the queue and bumps a generation counter so the
    writer aborts mid-chunk within ~one write frame (~20 ms), without calling
    PortAudio abort (which races with in-flight write on some hosts).
    """

    def __init__(self, *, sample_rate: int = 24_000, channels: int = 1) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._queue: queue.Queue[_QueueItem | None] = queue.Queue()
        self._stream: sd.RawOutputStream | None = None
        self._thread: threading.Thread | None = None
        self._bytes_queued = 0
        self._bytes_played = 0
        self._lock = threading.Lock()
        self._playing = False
        self._playing_kind: str | None = None
        self._empty = threading.Event()
        self._empty.set()
        self._generation = 0
        # ~20 ms frames — bounds max local stop latency after interrupt().
        self._write_frame_bytes = max(2, int(sample_rate * 0.02) * 2 * channels)
        self._last_interrupt_at = 0.0
        self._volume = 1.0
        # (monotonic time, rms) of frames handed to the speakers — the echo reference.
        self._out_levels: deque[tuple[float, float]] = deque()

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

    def enqueue(self, pcm: bytes, *, kind: str = "assistant") -> None:
        if not pcm:
            return
        with self._lock:
            gen = self._generation
            self._bytes_queued += len(pcm)
            self._empty.clear()
        self._queue.put((pcm, gen, kind))

    def set_volume(self, volume: float) -> None:
        """Software gain 0..1 applied per write frame (ducking without clearing queue)."""
        v = max(0.0, min(1.0, float(volume)))
        with self._lock:
            self._volume = v

    @property
    def volume(self) -> float:
        with self._lock:
            return self._volume

    def interrupt(self) -> int:
        """Drain not-yet-played audio (barge-in). Returns new generation id."""
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._last_interrupt_at = time.monotonic()
            self._volume = 1.0
        drained = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._queue.put(None)
                break
            pcm, _item_gen, _kind = item
            drained += len(pcm)
            with self._lock:
                self._bytes_queued = max(0, self._bytes_queued - len(pcm))
        with self._lock:
            if self._queue.empty() and not self._playing:
                self._empty.set()
        if drained:
            logger.info("BARGE_IN audio_queue_cleared drained_bytes=%s generation=%s", drained, gen)
        return gen

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
    def playing_kind(self) -> str | None:
        with self._lock:
            return self._playing_kind

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def recent_output_rms(self, window_s: float = 0.5) -> float:
        """Loudest frame played in the last `window_s` (covers output buffering + the
        speaker→mic delay). 0 when nothing has played recently."""
        cutoff = time.monotonic() - window_s
        with self._lock:
            return max((rms for at, rms in self._out_levels if at >= cutoff), default=0.0)

    def _note_output_level(self, frame: bytes) -> None:
        arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
        now = time.monotonic()
        with self._lock:
            self._out_levels.append((now, rms))
            while self._out_levels and self._out_levels[0][0] < now - 1.0:
                self._out_levels.popleft()

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
            self._playing_kind = None
            self._empty.set()

    def _write_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            pcm, gen, kind = item
            with self._lock:
                current_gen = self._generation
            if gen != current_gen:
                with self._lock:
                    self._bytes_queued = max(0, self._bytes_queued - len(pcm))
                    if self._queue.empty() and not self._playing:
                        self._empty.set()
                continue
            with self._lock:
                self._playing = True
                self._playing_kind = kind
            try:
                offset = 0
                while offset < len(pcm):
                    with self._lock:
                        if self._generation != gen:
                            break
                    frame = pcm[offset : offset + self._write_frame_bytes]
                    if not frame:
                        break
                    with self._lock:
                        vol = self._volume
                    if self._stream is not None:
                        if vol < 0.999:
                            arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                            arr = (arr * vol).clip(-32768, 32767).astype(np.int16)
                            frame = arr.tobytes()
                        self._note_output_level(frame)
                        self._stream.write(frame)
                    offset += len(frame)
                    with self._lock:
                        self._bytes_played += len(frame)
                        self._bytes_queued = max(0, self._bytes_queued - len(frame))
                # If interrupted mid-chunk, account for unplayed remainder.
                if offset < len(pcm):
                    with self._lock:
                        self._bytes_queued = max(0, self._bytes_queued - (len(pcm) - offset))
            except Exception as exc:
                logger.warning("Playback write error: %s", exc)
            finally:
                with self._lock:
                    self._playing = False
                    self._playing_kind = None
                    if self._queue.empty() and self._bytes_queued == 0:
                        self._empty.set()
