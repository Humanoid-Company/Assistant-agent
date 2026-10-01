"""Local busy / thinking cues — no LLM round-trip for «Угу»."""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_BUSY_CUE_PHRASES: tuple[str, ...] = (
    "Угу.",
    "Секунду.",
    "Зараз.",
    "Мм, зараз.",
)

PlayCueFn = Callable[[str, bytes], Awaitable[None] | None]
SynthesizeFn = Callable[[str], bytes | None]


class BusyCueController:
    """Schedules short backchannels while tools are pending.

    Race safety: every schedule captures ``turn_id``. Stale timers from a
    previous turn / cancelled tool wait never play.
    """

    def __init__(
        self,
        *,
        play_cue: PlayCueFn,
        synthesize: SynthesizeFn,
        phrases: Sequence[str] = DEFAULT_BUSY_CUE_PHRASES,
        enabled: bool = True,
        first_delay_ms: int = 900,
        second_delay_ms: int = 3000,
        max_per_turn: int = 2,
    ) -> None:
        self._play_cue = play_cue
        self._synthesize = synthesize
        self._phrases = list(phrases) or list(DEFAULT_BUSY_CUE_PHRASES)
        self.enabled = enabled
        self.first_delay_ms = first_delay_ms
        self.second_delay_ms = second_delay_ms
        self.max_per_turn = max(1, max_per_turn)

        self._turn_id = 0
        self._pending_tools = 0
        self._cues_played = 0
        self._last_phrase: str | None = None
        self._pcm_cache: dict[str, bytes] = {}
        self._tasks: set[asyncio.Task] = set()
        self._cue_playing = False

    @property
    def turn_id(self) -> int:
        return self._turn_id

    @property
    def pending_tools(self) -> int:
        return self._pending_tools

    @property
    def cues_played(self) -> int:
        return self._cues_played

    @property
    def cue_playing(self) -> bool:
        return self._cue_playing

    def invalidate(self, *, reason: str) -> None:
        """Bump turn id so scheduled timers become no-ops."""
        self._turn_id += 1
        self.cancel(reason=reason)

    def on_tool_started(self) -> None:
        if not self.enabled:
            return
        was_idle = self._pending_tools == 0
        self._pending_tools += 1
        if was_idle:
            self._cues_played = 0
            self._schedule(self.first_delay_ms, which=1)

    def on_tool_finished(self) -> None:
        if self._pending_tools > 0:
            self._pending_tools -= 1
        if self._pending_tools == 0:
            self.cancel(reason="tool_completed")

    def on_final_response_starting(self) -> None:
        self.cancel(reason="final_response")

    def on_user_speech(self) -> None:
        self.invalidate(reason="user_speech")
        self._cue_playing = False

    def cancel(self, *, reason: str) -> None:
        if self._tasks:
            logger.info("BUSY_CUE cancelled reason=%s pending_tasks=%s", reason, len(self._tasks))
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._cue_playing = False

    def _schedule(self, delay_ms: int, *, which: int) -> None:
        turn = self._turn_id
        logger.info("BUSY_CUE scheduled delay_ms=%s which=%s turn_id=%s", delay_ms, which, turn)

        async def _fire() -> None:
            try:
                await asyncio.sleep(delay_ms / 1000.0)
            except asyncio.CancelledError:
                raise
            if turn != self._turn_id:
                logger.info("BUSY_CUE skipped reason=stale_turn expected=%s actual=%s", turn, self._turn_id)
                return
            if self._pending_tools <= 0:
                logger.info("BUSY_CUE skipped reason=tool_completed")
                return
            if self._cues_played >= self.max_per_turn:
                logger.info("BUSY_CUE skipped reason=max_per_turn")
                return
            phrase = self._pick_phrase()
            pcm = self._pcm_for(phrase)
            if not pcm:
                logger.info("BUSY_CUE skipped reason=no_pcm phrase=%r", phrase)
                return
            if turn != self._turn_id or self._pending_tools <= 0:
                return
            self._cue_playing = True
            self._cues_played += 1
            self._last_phrase = phrase
            logger.info("BUSY_CUE played phrase=%r which=%s", phrase, which)
            try:
                maybe = self._play_cue(phrase, pcm)
                if asyncio.iscoroutine(maybe):
                    await maybe
            finally:
                self._cue_playing = False
            if (
                which == 1
                and self._pending_tools > 0
                and self._cues_played < self.max_per_turn
                and turn == self._turn_id
            ):
                extra = max(0, self.second_delay_ms - self.first_delay_ms)
                self._schedule(extra if extra > 0 else self.second_delay_ms, which=2)

        task = asyncio.create_task(_fire())
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)

        task.add_done_callback(_done)

    def _pick_phrase(self) -> str:
        choices = [p for p in self._phrases if p != self._last_phrase] or list(self._phrases)
        return random.choice(choices)

    def _pcm_for(self, phrase: str) -> bytes | None:
        cached = self._pcm_cache.get(phrase)
        if cached:
            return cached
        try:
            pcm = self._synthesize(phrase)
        except Exception:
            logger.exception("BUSY_CUE synthesize failed phrase=%r", phrase)
            return None
        if pcm:
            self._pcm_cache[phrase] = pcm
        return pcm

    def preload(self) -> None:
        """Best-effort cache fill (call from a worker thread)."""
        if not self.enabled:
            return
        for phrase in self._phrases:
            if phrase in self._pcm_cache:
                continue
            try:
                pcm = self._synthesize(phrase)
            except Exception:
                logger.warning("BUSY_CUE preload failed phrase=%r", phrase)
                continue
            if pcm:
                self._pcm_cache[phrase] = pcm
                logger.info("BUSY_CUE preloaded phrase=%r bytes=%s", phrase, len(pcm))
