"""Dialogue history kept outside any voice session.

A GPT-Live session fixes its voice at startup, so changing the voice — or waking up again after
a long pause — means a new session. The conversation must survive that: this log collects the
transcript of every session (desktop: one per app run; web: one per browser) and seeds the next
session with it through Live's startup `input` history.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

# GPT-Live accepts at most 128 history messages and 8,192 rendered tokens. Cyrillic text runs
# about 3 characters per token; 15k characters leaves room for the message framing.
_LIVE_MAX_MESSAGES = 120
_LIVE_MAX_CHARS = 15_000
_KEEP_TURNS = 400


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str


class ConversationLog:
    """Thread-safe transcript fed by streaming transcript deltas."""

    def __init__(self) -> None:
        self._turns: list[Turn] = []
        self._lock = threading.Lock()

    def add(self, role: str, delta: str) -> None:
        """Append a transcript fragment; a change of speaker starts a new turn."""
        if role not in ("user", "assistant") or not delta:
            return
        with self._lock:
            if self._turns and self._turns[-1].role == role:
                self._turns[-1].text += delta
            elif delta.strip():
                self._turns.append(Turn(role, delta.lstrip()))
                del self._turns[:-_KEEP_TURNS]

    def add_note(self, text: str) -> None:
        """Context she got without anyone saying it to her (overheard during a pause). Kept as her
        own remark, so later sessions still have it and it is never put in the user's mouth."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._turns.append(Turn("assistant", f"(Почула фоном під час паузи: {text})"))
            self._turns.append(Turn("boundary", ""))
            del self._turns[:-_KEEP_TURNS]

    def end_turn(self) -> None:
        """Close the current turn so the next fragment of the same speaker starts a new one."""
        with self._lock:
            if self._turns and self._turns[-1].role != "boundary":
                self._turns.append(Turn("boundary", ""))

    def turns(self) -> list[Turn]:
        with self._lock:
            return [Turn(t.role, t.text.strip()) for t in self._turns if t.role != "boundary" and t.text.strip()]

    def __len__(self) -> int:
        return len(self.turns())

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    def live_input(self, *, max_messages: int = _LIVE_MAX_MESSAGES, max_chars: int = _LIVE_MAX_CHARS) -> list[dict]:
        """The most recent turns as Live startup history (oldest first, within Live's limits)."""
        picked: list[Turn] = []
        used = 0
        for turn in reversed(self.turns()):
            if len(picked) >= max_messages or used + len(turn.text) > max_chars:
                break
            picked.append(turn)
            used += len(turn.text)
        picked.reverse()
        items = []
        for turn in picked:
            part = (
                {"type": "input_text", "text": turn.text}
                if turn.role == "user"
                else {"type": "output_text", "text": turn.text}
            )
            items.append({"type": "message", "role": turn.role, "content": [part]})
        return items
