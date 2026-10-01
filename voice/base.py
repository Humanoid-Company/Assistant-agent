"""Voice session abstraction — assistant must not depend on Realtime/Live event names."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class VoiceSession(Protocol):
    """Lifecycle interface used by Assistant while awake."""

    def connect(
        self,
        instructions: str,
        *,
        mic_read_chunk: Callable[[], bytes],
        backend_instructions: str | None = None,
    ) -> None:
        """Open the voice connection and start streaming microphone audio."""
        ...

    def close(self) -> None:
        """Gracefully stop playback, cancel tasks, and close the connection."""
        ...

    def run_until_idle(self) -> None:
        """Block until sleep/voice-restart is requested or the session fails.

        Realtime implements this with the existing pump loop expectations via
        Assistant orchestration; Live implements a full duplex event loop.
        """
        ...

    def append_instruction(self, text: str) -> None:
        """Add system-level behavior guidance for the conversation model."""
        ...

    def speak_context(self, text: str) -> None:
        """Ask the voice layer to communicate context aloud (engine-specific)."""
        ...

    def stop_playback(self) -> None:
        """Interrupt/drain local speaker queue only — does not cancel backend work."""
        ...

    def get_turns(self) -> list[dict]:
        """Accumulated user/assistant transcript turns for this awake session."""
        ...

    def request_sleep(self) -> None:
        """Signal the session to wind down (end_conversation)."""
        ...

    @property
    def sleep_requested(self) -> bool:
        ...

    @property
    def voice_restart_requested(self) -> bool:
        """True when voice change requires a new session (Live) or process exit (Realtime)."""
        ...
