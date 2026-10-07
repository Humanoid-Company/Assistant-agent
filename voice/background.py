"""What people nearby say while Єва is paused («Дякую, Єва»).

A pause means nobody is talking TO her — she is not switched off. The recognisers that already run
during a pause to catch «Єва, скажи» (browser speech recognition on the web, Google STT on the
desktop) also give the text of everything else, at no extra audio cost. The gist is kept here and
handed to her on wake, so she can remind the user of it later.

Measured on GPT-Live: a transcript sent as one commentary on wake was recalled 4/4; the same text
in the startup history or instructions was ignored. A commentary takes at most 500 tokens, so the
note is compressed to fit. Overheard text is never logged.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

# ≈ 280 tokens of Cyrillic (≈ 2.7 chars/token): with the wrapper (~100 tokens) and the wake
# greeting (~80) it still fits one 500-token commentary.
MAX_NOTE_CHARS = 750
# Raw text beyond this is folded into the running summary (≈ 8 minutes of talk).
_RAW_LIMIT_CHARS = 2400
_MIN_WORDS = 2  # «е», «ну» and lone noise words are not worth keeping

Summarize = Callable[[str], str]

_SUMMARY_PROMPT = (
    "Below is what people near a voice assistant said among themselves while it was paused "
    "(speech-recognition text, may contain errors). Write a compact note in Ukrainian, at most "
    f"{MAX_NOTE_CHARS - 100} characters, keeping every concrete fact: names, dates, times, places, "
    "numbers, decisions, tasks and promises. Skip small talk. Plain sentences, no lists or markdown.\n\n"
)


def make_openai_summarizer(client, model: str) -> Summarize:
    """Summarizer on the OpenAI Responses API (sync client)."""

    def summarize(text: str) -> str:
        response = client.responses.create(model=model, input=_SUMMARY_PROMPT + text)
        return (response.output_text or "").strip()

    return summarize


def wake_commentary(note: str) -> str:
    """Commentary that hands the note to Єва on wake (prepended to the greeting or the request)."""
    return (
        "While you were paused you kept listening in the background. People nearby said (among "
        "themselves, not to you): «" + note + "». Just remember it — do not retell it now unless asked; "
        "bring it up later when it helps (a reminder, a question about it). It was not said by the user "
        "to you, so never say «ти сказав» about it."
    )


class BackgroundLog:
    """Overheard text of one user's pauses: raw phrases plus a running summary."""

    def __init__(self, summarize: Summarize | None = None) -> None:
        self._summarize = summarize
        self._raw: list[str] = []
        self._summary = ""
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        text = " ".join((text or "").split())
        if len(text.split()) < _MIN_WORDS:
            return
        with self._lock:
            self._raw.append(text)
            too_long = sum(len(t) + 1 for t in self._raw) > _RAW_LIMIT_CHARS
        if too_long:
            self._fold()

    def digest(self) -> str:
        """Everything heard since the last digest as one note (≤ MAX_NOTE_CHARS); clears the log."""
        with self._lock:
            summary, raw = self._summary, " ".join(self._raw)
            self._summary, self._raw = "", []
        text = (summary + " " + raw).strip()
        if len(text) > MAX_NOTE_CHARS:
            text = self._compress(text)
        return text

    def clear(self) -> None:
        with self._lock:
            self._summary, self._raw = "", []

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._raw or self._summary)

    def _fold(self) -> None:
        with self._lock:
            text = (self._summary + " " + " ".join(self._raw)).strip()
            self._raw = []
        summary = self._compress(text)
        with self._lock:
            # Phrases added while compressing stay raw; the summary covers what came before them.
            self._summary = summary

    def _compress(self, text: str) -> str:
        if self._summarize is not None:
            try:
                summary = self._summarize(text)
                if summary:
                    logger.info("background.summarized chars_in=%s chars_out=%s", len(text), len(summary))
                    return summary[:MAX_NOTE_CHARS]
            except Exception as exc:
                logger.warning("background.summarize_failed: %s", type(exc).__name__)
        # No summarizer (or it failed): keep the most recent talk.
        return "…" + text[-(MAX_NOTE_CHARS - 1):]
