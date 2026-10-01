"""Per-user / per-session slot-filling state for calendar create.

Keeps title, date, time and duration across multi-turn voice clarification
without letting the model invent new values or leak drafts across Google accounts.
"""
from __future__ import annotations

import re
import threading
import time as time_module
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

DEFAULT_DRAFT_TTL_S = 600.0  # 10 minutes for an unfinished create

_YES_TITLE = re.compile(
    r"^\s*(так|да|yes|правильно|підходить|вірно|згоден|згодна|ок|окей|добре|гаразд)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_CANCEL_DRAFT = re.compile(
    r"^\s*(скасуй|скасувати|не треба|забудь|cancel|нічого|стоп)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_ACK_ONLY = re.compile(
    r"^(дякую|дякую тобі|спасибі|спасибо|алло|ало|hello|хай|агов|"
    r"ок|окей|ага|угу|добре|гаразд|зрозумів|поняв|thanks|thank you)[\s.!?]*$",
    re.IGNORECASE,
)
# Strip common Ukrainian / Russian inflection endings for fuzzy title match.
_STEM_ENDINGS = (
    "ою",
    "ею",
    "ами",
    "ями",
    "ів",
    "їв",
    "ей",
    "ою",
    "ом",
    "ем",
    "ею",
    "а",
    "я",
    "у",
    "ю",
    "и",
    "і",
    "і",
    "е",
    "є",
    "о",
    "ь",
)


def is_title_confirmation(text: str) -> bool:
    return bool(_YES_TITLE.fullmatch((text or "").strip()))


def is_draft_cancel(text: str) -> bool:
    return bool(_CANCEL_DRAFT.fullmatch((text or "").strip()))


def is_acknowledgement(text: str) -> bool:
    return bool(_ACK_ONLY.fullmatch((text or "").strip()))


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").casefold().strip()
    return re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)


def _stem_token(token: str) -> str:
    if len(token) <= 3:
        return token
    for ending in sorted(_STEM_ENDINGS, key=len, reverse=True):
        if token.endswith(ending) and len(token) - len(ending) >= 3:
            return token[: -len(ending)]
    return token


def _tokens(text: str) -> list[str]:
    return [_stem_token(part) for part in _fold(text).split() if part]


def title_similarity(left: str, right: str) -> float:
    a = " ".join(_tokens(left))
    b = " ".join(_tokens(right))
    if not a or not b:
        return 0.0
    if a == b or a in _fold(right) or b in _fold(left):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def title_said_by_user(title: str | None, utterances: list[str], *, min_ratio: float = 0.82) -> bool:
    """True when the candidate title appears in user speech (exact or light inflection)."""
    needle = (title or "").strip()
    if not needle:
        return False
    blob = " ".join(utterances)
    folded_blob = _fold(blob)
    folded_title = _fold(needle)
    if folded_title and folded_title in folded_blob:
        return True
    title_tokens = _tokens(needle)
    if not title_tokens:
        return False
    # Whole-title fuzzy against each utterance and the full blob.
    for text in (*utterances, blob):
        if title_similarity(needle, text) >= min_ratio:
            return True
    # Contiguous stemmed-token window inside the blob.
    blob_tokens = _tokens(blob)
    width = len(title_tokens)
    if width and width <= len(blob_tokens):
        for start in range(len(blob_tokens) - width + 1):
            window = " ".join(blob_tokens[start : start + width])
            if SequenceMatcher(None, " ".join(title_tokens), window).ratio() >= min_ratio:
                return True
    return False


def best_title_span(utterances: list[str]) -> str | None:
    """Pick the latest non-ack utterance as a candidate title phrase."""
    for text in reversed(utterances):
        cleaned = (text or "").strip()
        if not cleaned or is_acknowledgement(cleaned) or is_title_confirmation(cleaned):
            continue
        if is_draft_cancel(cleaned):
            continue
        # Prefer short clarifying lines over the original long request.
        if len(cleaned) <= 80:
            return cleaned
        return cleaned
    return None


@dataclass
class CreateDraft:
    user_sub: str
    session_id: str
    title: str | None = None
    date: str | None = None
    time: str | None = None
    duration_minutes: int | None = None
    timezone: str | None = None
    proposed_title: str | None = None
    confirmed_fields: set[str] = field(default_factory=set)
    updated_at: float = field(default_factory=time_module.time)
    expires_at: float = 0.0

    def touch(self, ttl: float) -> None:
        self.updated_at = time_module.time()
        self.expires_at = self.updated_at + ttl

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time_module.time()) > self.expires_at

    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.title:
            missing.append("title")
        if not self.date:
            missing.append("date")
        if not self.time:
            missing.append("time")
        return missing


class CreateDraftStore:
    """In-memory drafts keyed by (google_sub, voice_session_id)."""

    def __init__(self, ttl_seconds: float = DEFAULT_DRAFT_TTL_S) -> None:
        self._ttl = ttl_seconds
        self._by_key: dict[tuple[str, str], CreateDraft] = {}
        self._lock = threading.RLock()

    def _key(self, user_sub: str, session_id: str | None) -> tuple[str, str]:
        return (user_sub, session_id or "")

    def get(self, user_sub: str, session_id: str | None) -> CreateDraft | None:
        with self._lock:
            key = self._key(user_sub, session_id)
            draft = self._by_key.get(key)
            if draft is None:
                return None
            if draft.user_sub != user_sub or draft.is_expired():
                self._by_key.pop(key, None)
                return None
            return draft

    def get_or_create(self, user_sub: str, session_id: str | None) -> CreateDraft:
        with self._lock:
            existing = self.get(user_sub, session_id)
            if existing is not None:
                return existing
            draft = CreateDraft(user_sub=user_sub, session_id=session_id or "")
            draft.touch(self._ttl)
            self._by_key[self._key(user_sub, session_id)] = draft
            return draft

    def save(self, draft: CreateDraft) -> CreateDraft:
        with self._lock:
            draft.touch(self._ttl)
            self._by_key[self._key(draft.user_sub, draft.session_id)] = draft
            return draft

    def clear(self, user_sub: str, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                doomed = [key for key in self._by_key if key[0] == user_sub]
                for key in doomed:
                    self._by_key.pop(key, None)
                return
            self._by_key.pop(self._key(user_sub, session_id), None)

    def clear_all(self) -> None:
        with self._lock:
            self._by_key.clear()
