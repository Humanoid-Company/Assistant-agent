"""
Text-to-speech for the single startup line, before the wake phrase is heard.

The conversation itself is voiced by the voice-engine session — GPT-Live
(`voice/live_session.py`, default) or legacy Realtime (`realtime_client.py`) —
this module only covers the moment before that session exists.
"""
import io
import logging

import pygame
from openai import OpenAI

from config import OPENAI_API_KEY, TTS_MODEL, TTS_VOICE

logger = logging.getLogger(__name__)


class TextToSpeech:
    """Synthesise and play a short line of Ukrainian speech to completion."""

    def __init__(self) -> None:
        self._openai = OpenAI(api_key=OPENAI_API_KEY)
        pygame.mixer.init(frequency=22050, size=-16, channels=1, buffer=1024)

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        audio_buf = self._synthesize(text)
        if audio_buf:
            self._play(audio_buf)

    def cleanup(self) -> None:
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except Exception:
            pass

    def _synthesize(self, text: str) -> io.BytesIO | None:
        try:
            response = self._openai.audio.speech.create(
                model=TTS_MODEL,
                voice=TTS_VOICE,
                input=text,
                response_format="mp3",
            )
            buf = io.BytesIO(response.content)
            buf.seek(0)
            return buf
        except Exception as exc:
            logger.error("OpenAI TTS error: %s", exc)
            return None

    def _play(self, audio_buf: io.BytesIO) -> None:
        try:
            audio_buf.seek(0)
            pygame.mixer.music.load(audio_buf, namehint=".mp3")
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.wait(20)
        except Exception as exc:
            logger.error("Playback error: %s", exc)
