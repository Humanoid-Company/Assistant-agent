"""User-changeable voice settings: preset voices, languages, assistant-name validation."""
from __future__ import annotations

import re
from dataclasses import dataclass

# The Realtime API only accepts these ten preset voice IDs — no custom voices,
# no cloning. Exposed to the model as an enum (change_voice in tools/*_schemas.py) so
# IT does the mapping from whatever the user actually said ("постав жіночий
# голос", "хочу голос марін") to one of these — far more robust than us
# hand-rolling a Ukrainian-phonetic-spelling lookup table for English names.
VOICE_OPTIONS: tuple[str, ...] = (
    "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar",
)


@dataclass(frozen=True)
class VoicePersona:
    """One entry of the web voice picker: a preset voice + how it should sound."""

    voice: str  # one of VOICE_OPTIONS — what the API actually gets
    label: str
    description: str  # shown under the picker
    style: str  # appended to the Live instructions: delivery, tempo, tone


# Voices offered in the web picker, in display order. Keyed by the preset voice id, so a
# stored WebUser.voice and the change_voice tool argument stay plain voice ids.
VOICE_PERSONAS: dict[str, VoicePersona] = {
    p.voice: p
    for p in (
        VoicePersona(
            "marin",
            "Марина",
            "Жіночий, теплий і природний. Звучить як уважна помічниця — голос за замовчуванням.",
            "Speak warmly and naturally, at a relaxed conversational pace.",
        ),
        VoicePersona(
            "coral",
            "Корал",
            "Жіночий, жвавий і дружній. Бадьора інтонація, говорить трохи швидше.",
            "Sound upbeat and friendly, with lively intonation and a slightly brisk pace.",
        ),
        VoicePersona(
            "shimmer",
            "Шиммер",
            "Жіночий, м'який і спокійний. Тихіша, заспокійлива манера — для вечора чи довгих розмов.",
            "Speak softly and calmly, unhurried, with a soothing gentle tone.",
        ),
        VoicePersona(
            "sage",
            "Сейдж",
            "Жіночий, зібраний і діловий. Чітко, рівно, без зайвих емоцій — для роботи з календарем і поштою.",
            "Speak clearly and evenly, in a composed, businesslike manner; keep it crisp.",
        ),
        VoicePersona(
            "cedar",
            "Кедр",
            "Чоловічий, глибокий і впевнений. Спокійний низький тембр.",
            "Speak in a calm, confident, grounded manner at a measured pace.",
        ),
    )
}

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
