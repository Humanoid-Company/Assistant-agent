"""User-changeable voice settings: preset voices, languages, assistant-name validation."""
from __future__ import annotations

import re

# The Realtime API only accepts these ten preset voice IDs — no custom voices,
# no cloning. Exposed to the model as an enum (change_voice in tools/*_schemas.py) so
# IT does the mapping from whatever the user actually said ("постав жіночий
# голос", "хочу голос марін") to one of these — far more robust than us
# hand-rolling a Ukrainian-phonetic-spelling lookup table for English names.
VOICE_OPTIONS: tuple[str, ...] = (
    "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar",
)

# Unlike voice, the spoken language is just plain-text instructions + an STT
# transcription hint — both apply live via a session.update, no reconnect
# needed (see RealtimeConversation.update_transcription_language()).
LANGUAGE_OPTIONS: dict[str, str] = {
    "uk": "українською",
    "ru": "російською",
    "en": "англійською",
}

# Words that are never a plausible name — pronouns/fillers/verbs a misheard or
# noisy transcript (or a too-trusting model tool-call argument) sometimes
# produces (e.g. "ти", "ты", a bare single letter). Rejecting these outright
# stops bogus names from being accepted.
_NOT_A_NAME = {
    "ти", "ты", "я", "він", "вона", "воно", "они", "вони", "ми", "мы", "ви", "вы",
    "хто", "що", "це", "то", "так", "ні", "нет", "да", "ага", "ну", "тобто",
    "тут", "там", "де", "куди", "звідки", "коли", "чому", "навіщо", "як",
}
_MIN_NAME_LENGTH = 2

# Matches one word (Cyrillic/Latin letters + internal apostrophes, e.g. "Дем'ян")
# — used to strip surrounding punctuation before validating a name candidate
# coming from a tool-call argument.
_NAME_TOKEN_RE = re.compile(r"[а-щьюяєіїґА-ЩЬЮЯЄІЇҐa-zA-Z]+(?:'[а-щьюяєіїґА-ЩЬЮЯЄІЇҐa-zA-Z]+)*")


def _sanitize_name(raw: str) -> str:
    """Validate/clean a candidate name from a tool-call argument.

    Rejects bare pronouns/verbs, punctuation-only scraps, and single letters —
    returns "" if nothing plausible is found.
    """
    match = _NAME_TOKEN_RE.search(raw or "")
    if not match:
        return ""
    candidate = match.group(0).capitalize()
    if len(candidate) < _MIN_NAME_LENGTH or candidate.lower() in _NOT_A_NAME:
        return ""
    return candidate
