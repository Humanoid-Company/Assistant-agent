"""Engine selection for voice sessions."""
from __future__ import annotations

import logging
from typing import Any, Callable, Literal

from config import VOICE_ENGINE

logger = logging.getLogger(__name__)

VoiceEngineName = Literal["live", "realtime"]


def normalize_voice_engine(value: str | None) -> VoiceEngineName:
    raw = (value or VOICE_ENGINE or "realtime").strip().lower()
    if raw in ("live", "gpt-live", "gpt_live"):
        return "live"
    return "realtime"


def create_voice_session(
    engine: str | None = None,
    **kwargs: Any,
) -> Any:
    """Factory used by Assistant and tests.

    Returns LiveVoiceSession or RealtimeLegacySession depending on VOICE_ENGINE.
    """
    name = normalize_voice_engine(engine)
    if name == "live":
        from voice.live_session import LiveVoiceSession

        logger.info("voice.engine selected=live")
        return LiveVoiceSession(**kwargs)
    from realtime_client import RealtimeConversation
    from voice.realtime_legacy import RealtimeLegacySession

    logger.info("voice.engine selected=realtime")
    # Realtime path expects tools/on_tool_call — pull from kwargs.
    tools = kwargs.pop("tools")
    on_tool_call = kwargs.pop("on_tool_call")
    silent_tools = kwargs.pop("silent_tools", None)
    no_followup_tools = kwargs.pop("no_followup_tools", None)
    voice = kwargs.pop("voice", None)
    # Drop Live-only kwargs
    for key in (
        "tool_executor",
        "live_model",
        "backend_model",
        "audio_rate",
        "on_user_transcript",
        "session_id",
    ):
        kwargs.pop(key, None)
    inner = RealtimeConversation(
        tools=tools,
        on_tool_call=on_tool_call,
        silent_tools=silent_tools,
        no_followup_tools=no_followup_tools,
        voice=voice,
    )
    return RealtimeLegacySession(inner)
