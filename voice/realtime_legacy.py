"""Adapter wrapping the legacy RealtimeConversation behind VoiceSession-ish API.

Keeps all Realtime-specific workarounds inside realtime_client.py.
Assistant may still drive the pump loop directly via `.inner`.
"""
from __future__ import annotations

from typing import Callable, Optional

from realtime_client import RealtimeConversation


class RealtimeLegacySession:
    """Thin facade — does not rewrite RealtimeConversation internals."""

    def __init__(self, inner: RealtimeConversation) -> None:
        self.inner = inner
        self._sleep_requested = False
        self._voice_restart_requested = False

    def connect(
        self,
        instructions: str,
        *,
        mic_read_chunk: Callable[[], bytes],
        backend_instructions: str | None = None,
    ) -> None:
        del backend_instructions  # Realtime uses a single instruction blob
        self.inner.connect(instructions, mic_read_chunk=mic_read_chunk)

    def close(self) -> None:
        self.inner.close()

    def run_until_idle(self) -> None:
        # Orchestration stays in Assistant for Realtime (pump / create_response).
        raise NotImplementedError("RealtimeLegacySession is driven by Assistant._run_awake_session_realtime")

    def append_instruction(self, text: str) -> None:
        # Realtime replaces full instructions rather than appending.
        self.inner.update_instructions(text)

    def speak_context(self, text: str) -> None:
        self.inner.say(text)

    def stop_playback(self) -> None:
        self.inner.player.stop()

    def get_turns(self) -> list[dict]:
        return self.inner.get_turns()

    def request_sleep(self) -> None:
        self._sleep_requested = True

    @property
    def sleep_requested(self) -> bool:
        return self._sleep_requested

    @property
    def voice_restart_requested(self) -> bool:
        return self._voice_restart_requested

    def mark_voice_restart(self) -> None:
        self._voice_restart_requested = True
        self._sleep_requested = True
