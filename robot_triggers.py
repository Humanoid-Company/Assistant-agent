"""Spoken robot commands matched locally (fast path that bypasses the model)."""
from __future__ import annotations

import re

from config import (
    ROBOT_TRIGGER_PHRASES,
)

# Matches ROBOT_TRIGGER_PHRASES as whole words/phrases, longest phrase first
# (so "поверни ліворуч" wins over the bare "ліворуч" when both are present) —
# checked against the local fast-STT transcript before the turn ever reaches
# the model, so a robot command always executes instead of risking the model
# deciding to chat/joke about it instead of calling control_robot.
_ROBOT_TRIGGER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{re.escape(phrase)}\b"), action)
    for phrase, action in sorted(ROBOT_TRIGGER_PHRASES.items(), key=lambda kv: -len(kv[0]))
]


def _match_robot_trigger(text: str) -> str | None:
    for pattern, action in _ROBOT_TRIGGER_PATTERNS:
        if pattern.search(text):
            return action
    return None


# Spoken confirmation for a matched trigger phrase or a control_robot tool
# call — same text either way.
_ROBOT_ACTION_TEXT: dict[str, str] = {
    "move_forward": "Іду вперед.",
    "move_backward": "Іду назад.",
    "turn_left": "Повертаю ліворуч.",
    "turn_right": "Повертаю праворуч.",
    "stop": "Зупиняюсь.",
    "sit": "Сідаю.",
    "stand_up": "Встаю.",
    "stand_down": "Лягаю.",
    "greet": "Вітаюсь.",
}
