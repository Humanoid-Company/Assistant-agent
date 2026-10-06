"""Real GPT-Live check: a session started with another voice remembers the earlier conversation.

Opt-in (costs a few cents, needs network and OPENAI_API_KEY):  RUN_LIVE_TESTS=1 pytest -q -k live_memory
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values

from config import OPENAI_LIVE_MODEL
from prompts.live_prompt import build_live_prompt
from voice.conversation import ConversationLog

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to call GPT-Live")


async def _ask(log: ConversationLog, voice: str, question: str) -> str:
    from openai import AsyncOpenAI

    # conftest.py puts a dummy key into the environment; the real one is in .env.
    key = dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("OPENAI_API_KEY")
    client = AsyncOpenAI(api_key=key)
    said: list[str] = []
    async with client.live.connect() as conn:
        await conn.session.start(
            session={
                "model": OPENAI_LIVE_MODEL,
                "instructions": build_live_prompt(
                    language_name="українською", assistant_name=None, today="2026-10-06", voice=voice
                ),
                "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": voice}},
                "input": log.live_input(),
            },
            event_id="start",
        )
        started = time.monotonic()
        last = 0.0

        async def reader():
            nonlocal last
            async for event in conn:
                etype = getattr(event, "type", "")
                if etype == "session.started":
                    await conn.session.commentary.append(
                        content=f"The user asks: «{question}» Answer briefly.", delegation_id=None, event_id="q"
                    )
                elif etype == "session.output_transcript.delta":
                    said.append(event.delta)
                    last = time.monotonic()

        async def silence():  # Live expects a live microphone stream
            chunk = base64.b64encode(b"\x00\x00" * 480).decode()
            while True:
                await conn.session.input_audio.append(audio=chunk)
                await asyncio.sleep(0.02)

        tasks = [asyncio.create_task(reader()), asyncio.create_task(silence())]
        while time.monotonic() - started < 30 and not (last and time.monotonic() - last > 2.5):
            await asyncio.sleep(0.2)
        for task in tasks:
            task.cancel()
    return "".join(said)


def test_live_memory_survives_voice_change():
    log = ConversationLog()
    log.add("user", "Мене звати Остап, і я п'ю каву без цукру. Говоримо про поїздку до Львова в суботу.")
    log.add("assistant", "Приємно, Остапе! Запам'ятала: кава без цукру, Львів у суботу.")
    log.add("user", "Зміни голос на чоловічий.")
    log.add("assistant", "Перемикаю голос.")
    answer = _run(_ask(log, "meridian", "Як мене звати, яку каву я п'ю і куди ми їдемо?")).lower()
    assert "остап" in answer
    assert "цукр" in answer
    assert "львов" in answer or "львів" in answer


def _run(coro):
    return asyncio.run(coro)
