"""Split prompts for GPT-Live conversation vs Responses backend."""
from prompts.backend_prompt import BACKEND_PROMPT, build_backend_prompt
from prompts.live_prompt import LIVE_PROMPT, build_live_prompt

__all__ = [
    "LIVE_PROMPT",
    "BACKEND_PROMPT",
    "build_live_prompt",
    "build_backend_prompt",
]
