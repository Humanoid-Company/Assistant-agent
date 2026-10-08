"""Waking Єва («Єва, скажи», «Привіт, Єва», «Гей, Єва», …) and pausing her (only «Дякую, Єва»)
— tolerant matching of STT transcripts.

Speech recognisers write the name many ways («Єва», «Єво», «Ева», «Eva», «є ва») and mangle
short words («скажі», «кажи», «дякуєм»). The name is matched against an explicit list — fuzzy
matching a three-letter word would also accept «два» or «нова» — and the verbs fuzzily.
web/app.js has a JavaScript copy of these rules (EVA_*); keep the two in sync.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

AGENT_NAME = "Єва"

# Every form of the name an STT may produce, already normalised (see _tokens). Only the forms
# used to address her: «скажи Єві» / «я скажу Єву» talk ABOUT her and must not wake her.
_NAME_FORMS = frozenset({
    "єва", "єво",
    "ева", "эва", "эво", "ево", "єфа", "ефа",
    "їва", "іва", "йева", "йєва", "єєва",  # Chrome's uk-UA spellings of a quick «Єва»
    "eva", "evo", "eve", "yeva", "yevo", "jeva",
})
# The name plus one of these wakes her. The name alone does not: talking ABOUT Єва mustn't.
_WAKE_VERBS = (
    "скажи", "кажи", "скажіть", "скажи-но",
    "привіт", "привітик", "вітаю", "гей", "хей", "агов", "алло",
    "слухай", "послухай", "допоможи", "підкажи", "прокидайся",
    "hello", "hey",
)
# One pause phrase on purpose — «Дякую, Єва»; the variants are only STT spellings of «дякую».
_STOP_VERBS = ("дякую", "дякуємо")
_MAX_GAP = 2  # words allowed between the name and the verb («Єва, ну скажи»)

_WORD_RE = re.compile(r"[a-zа-яіїєґё'’-]+")


def _tokens(text: str) -> list[str]:
    text = (text or "").lower().replace("’", "'").replace("ё", "е")
    words = [w.strip("'-") for w in _WORD_RE.findall(text)]
    words = [w for w in words if w]
    # «є ва» / «е ва»: the recogniser split the name in two.
    merged: list[str] = []
    for word in words:
        if merged and merged[-1] in ("є", "е", "э") and word == "ва":
            merged[-1] += word
        else:
            merged.extend(_unglue(word))
    return merged


def _unglue(word: str) -> list[str]:
    """«привітєва» / «гейєва» / «євоскажи»: the recogniser glued the name to the verb — split them."""
    if len(word) <= 4 or word in _NAME_FORMS:
        return [word]
    verbs = _WAKE_VERBS + _STOP_VERBS
    for size in (4, 3):
        if word[-size:] in _NAME_FORMS and _like(word[:-size], verbs):
            return [word[:-size], word[-size:]]
        if word[:size] in _NAME_FORMS and _like(word[size:], verbs):
            return [word[:size], word[size:]]
    return [word]


def _is_name(word: str) -> bool:
    return word in _NAME_FORMS


def _like(word: str, verbs: tuple[str, ...], ratio: float = 0.75) -> bool:
    return any(word == v or SequenceMatcher(None, word, v).ratio() >= ratio for v in verbs)


def _name_and_verb(words: list[str], verbs: tuple[str, ...]) -> tuple[int, int] | None:
    """Indexes (name, verb) of the first name/verb pair at most _MAX_GAP words apart."""
    for i, word in enumerate(words):
        if not _is_name(word):
            continue
        lo, hi = max(0, i - _MAX_GAP - 1), min(len(words), i + _MAX_GAP + 2)
        for j in range(lo, hi):
            if j != i and _like(words[j], verbs):
                return i, j
    return None


def addresses_eva(text: str) -> bool:
    """The text names her («Єва», «Єво», «Ева»…) — said to her or about her, not background talk."""
    return any(_is_name(word) for word in _tokens(text))


def match_wake(text: str) -> str | None:
    """«Єва, скажи …» / «Привіт, Єва …» → what was said after the phrase ("" if nothing); None if absent."""
    words = _tokens(text)
    pair = _name_and_verb(words, _WAKE_VERBS)
    if pair is None:
        return None
    return " ".join(words[max(pair) + 1:])


def is_stop(text: str) -> bool:
    """«Дякую, Єва» / «Єво, дякую» / «дякую Єва» in the text — the only pause phrase."""
    return _name_and_verb(_tokens(text), _STOP_VERBS) is not None
