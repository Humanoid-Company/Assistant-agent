"""Does the user actually want to interrupt the assistant? Decided from partial transcripts.

The robot talks to live audiences, mostly over laptop/robot speakers without hardware echo
cancellation. While it speaks, the input transcript also catches listeners' backchannels
("угу", "ага"), laughter, side conversations and the assistant's own voice coming back
through the mic. None of those should cut it off. Only a stop word or the user clearly
taking the turn should.
"""
from __future__ import annotations

import re
from typing import Literal

InterruptIntent = Literal["stop", "takeover", "backchannel", "echo", "unclear"]

# Explicit "stop talking" phrases (uk/ru/en) — interrupt immediately, reply with a short ack.
_STOP_PHRASES = (
    "стоп",
    "стій",
    "стой",
    "зачекай",
    "зачекайте",
    "почекай",
    "почекайте",
    "чекай",
    "подожди",
    "погоди",
    "тихо",
    "досить",
    "хватит",
    "не треба",
    "перестань",
    "замовкни",
    "помовч",
    "секунду",
    "секундочку",
    "хвилинку",
    "минутку",
    "stop",
    "wait",
    "hold on",
)
# Openers people use to grab the turn ("слухай, …") — a takeover even before more words land.
_TAKEOVER_OPENERS = ("слухай", "послухай", "стривай", "та ні", "ні ні", "ні-ні", "нет нет", "а ще", "скажи")

# Listener feedback that must not stop the speaker.
_BACKCHANNELS = frozenset(
    {
        "угу", "ага", "ага-ага", "мгм", "ммм", "мм", "м", "хм", "ну", "так", "так-так", "да", "ну да",
        "ок", "окей", "добре", "ясно", "зрозуміло", "ого", "вау", "ух", "ха", "хаха", "ахах",
        "ага-а", "ееее", "е", "а", "о", "ой", "оу", "клас", "супер", "круто", "правда", "справді",
        "серйозно", "точно", "звісно", "yeah", "yes", "ok", "okay", "uh-huh", "mhm", "wow",
    }
)
_WORD_RE = re.compile(r"[\w'’ʼ-]+", re.UNICODE)
_TAKEOVER_MIN_WORDS = 2
_ECHO_OVERLAP = 0.6


def _words(text: str) -> list[str]:
    cleaned = (text or "").lower().replace("’", "'").replace("ʼ", "'")
    return _WORD_RE.findall(cleaned)


def _has_phrase(words: list[str], phrases: tuple[str, ...]) -> bool:
    joined = " " + " ".join(words) + " "
    return any(f" {phrase} " in joined for phrase in phrases)


def _is_echo(words: list[str], assistant_recent: str) -> bool:
    """Most of what the mic 'heard' is what the assistant itself just said."""
    content = [w for w in words if len(w) >= 3]
    if len(content) < 2 or not assistant_recent:
        return False
    spoken = set(_words(assistant_recent))
    overlap = sum(1 for w in content if w in spoken)
    return overlap / len(content) >= _ECHO_OVERLAP


def classify_interjection(text: str, *, assistant_recent: str = "") -> InterruptIntent:
    """Classify what the user said while the assistant was talking.

    `text` is the accumulated partial transcript of the current candidate utterance;
    `assistant_recent` is what the assistant has been saying (for echo detection).
    """
    words = _words(text)
    if not words:
        return "unclear"
    # Echo first: the assistant saying «скажіть стоп» must not stop itself via the mic.
    if _is_echo(words, assistant_recent):
        return "echo"
    if _has_phrase(words, _STOP_PHRASES):
        return "stop"
    if _has_phrase(words, _TAKEOVER_OPENERS):
        return "takeover"
    if " ".join(words) in _BACKCHANNELS or all(w in _BACKCHANNELS for w in words):
        return "backchannel"
    meaningful = [w for w in words if w not in _BACKCHANNELS]
    if len(meaningful) >= _TAKEOVER_MIN_WORDS:
        return "takeover"
    return "unclear"
