"""ElevenLabs voice for the Realtime engine (server/realtime_engine.py).

With «ElevenLabs» chosen, Realtime answers in text and the page has each sentence voiced through
/api/eleven/tts — the ElevenLabs key stays here. /api/eleven/voices lists the account's voices.
"""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import BaseModel

from config import ELEVENLABS_API_KEY

logger = logging.getLogger(__name__)

ELEVEN_API = "https://api.elevenlabs.io"
# Offered on the page. Flash v2.5 is the default: known to take every setting sent here.
ELEVEN_MODELS: dict[str, str] = {
    "eleven_flash_v2_5": "Flash v2.5 — швидка, перевірена",
    "eleven_v4_turbo": "v4 Turbo — швидка й емоційна (нова)",
    "eleven_v4": "v4 — найживіша (нова)",
    "eleven_v3_conversational": "v3 Conversational — для розмови",
    "eleven_turbo_v2_5": "Turbo v2.5",
    "eleven_multilingual_v2": "Multilingual v2 — стабільна",
    "eleven_v3": "v3 — виразна, повільна",
}
# language_code (forcing Ukrainian) is rejected by multilingual_v2 only.
_NO_LANGUAGE_CODE_MODELS = {"eleven_multilingual_v2"}
# Request stitching (previous_text) is documented for the v2 / v2.5 models; v3 and newer: unknown.
_PREVIOUS_TEXT_MODELS = {"eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2"}
# v3 takes only three stability steps (creative / natural / robust).
_STABILITY_STEPS_MODELS = {"eleven_v3", "eleven_v3_conversational"}
# One sentence or two at a time; a cap so a runaway reply can't burn the month's credits.
MAX_TTS_CHARS = 800
_VOICES_TTL_S = 600.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class TtsRequest(BaseModel):
    text: str
    voice_id: str
    model: str = "eleven_flash_v2_5"
    stability: float = 0.5
    similarity: float = 0.75
    style: float = 0.0
    speed: float = 1.0
    speaker_boost: bool = True
    previous_text: str = ""  # the sentence before: keeps the intonation joined up
    language: str = "uk"


def tts_payload(req: TtsRequest) -> dict:
    """ElevenLabs request body from the page's settings, every value kept in its allowed range."""
    model = req.model if req.model in ELEVEN_MODELS else "eleven_flash_v2_5"
    stability = _clamp(req.stability, 0.0, 1.0)
    if model in _STABILITY_STEPS_MODELS:
        stability = min((0.0, 0.5, 1.0), key=lambda step: abs(step - stability))
    payload: dict[str, Any] = {
        "text": req.text.strip()[:MAX_TTS_CHARS],
        "model_id": model,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": _clamp(req.similarity, 0.0, 1.0),
            "style": _clamp(req.style, 0.0, 1.0),
            "speed": _clamp(req.speed, 0.7, 1.2),
            "use_speaker_boost": req.speaker_boost,
        },
    }
    if req.previous_text.strip() and model in _PREVIOUS_TEXT_MODELS:
        payload["previous_text"] = req.previous_text.strip()[-MAX_TTS_CHARS:]
    if model not in _NO_LANGUAGE_CODE_MODELS and req.language in ("uk", "en", "ru"):
        payload["language_code"] = req.language
    return payload


class ElevenLabs:
    def __init__(self, api_key: str = ELEVENLABS_API_KEY) -> None:
        self.api_key = api_key
        self._http = httpx.AsyncClient(base_url=ELEVEN_API, timeout=httpx.Timeout(30.0, connect=10.0))
        self._voices: list[dict] = []
        self._voices_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def voices(self) -> list[dict]:
        """The account's voices (premade + added from the library), cached for a few minutes."""
        if self._voices and time.monotonic() - self._voices_at < _VOICES_TTL_S:
            return self._voices
        res = await self._http.get("/v1/voices", headers={"xi-api-key": self.api_key})
        res.raise_for_status()
        voices = []
        for v in res.json().get("voices", []):
            labels = v.get("labels") or {}
            voices.append({
                "id": v.get("voice_id"),
                "name": v.get("name") or "?",
                "gender": labels.get("gender") or "",
                "description": ", ".join(
                    str(labels[k]) for k in ("accent", "age", "description", "use_case", "descriptive") if labels.get(k)
                ),
                "category": v.get("category") or "",
            })
        self._voices = [v for v in voices if v["id"]]
        self._voices_at = time.monotonic()
        return self._voices

    async def tts(self, req: TtsRequest) -> AsyncIterator[bytes]:
        """MP3 chunks as ElevenLabs produces them. Raises before the first chunk on an API error,
        so the endpoint can still answer with a proper status."""
        payload = tts_payload(req)
        request = self._http.build_request(
            "POST",
            f"/v1/text-to-speech/{req.voice_id}/stream",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self.api_key},
            json=payload,
        )
        started = time.monotonic()
        response = await self._http.send(request, stream=True)
        if response.status_code != 200:
            detail = (await response.aread()).decode(errors="replace")[:300]
            await response.aclose()
            raise ElevenLabsError(response.status_code, detail)
        logger.info(
            "eleven.tts model=%s chars=%s headers_ms=%s", payload["model_id"], len(payload["text"]),
            int((time.monotonic() - started) * 1000),
        )

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return body()


class ElevenLabsError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"ElevenLabs {status}: {detail}")
        self.status = status
        self.detail = detail
