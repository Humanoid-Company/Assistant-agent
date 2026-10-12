"""Delivery prompt variants for GPT-Live: v2 (default), v1 (older), v3 (v2 + behaving like a person)."""
from __future__ import annotations

from prompts.live_prompt import LIVE_PROMPT, PROMPT_VARIANTS, build_live_prompt

COMMON = dict(language_name="українською", assistant_name=None, today="2026-10-09")


def test_variants():
    assert PROMPT_VARIANTS == ("v1", "v2", "v3")
    assert "you are talking, not reading" in build_live_prompt(**COMMON)  # v2 by default
    assert "you are talking, not reading" not in build_live_prompt(variant="v1", **COMMON)
    assert "Listening like a person" in LIVE_PROMPT


def test_v3_adds_person_to_v2():
    v2 = build_live_prompt(**COMMON)
    v3 = build_live_prompt(variant="v3", **COMMON)
    assert "Being a person" not in v2 and "Being a person" in v3
    assert "you are talking, not reading" in v3  # v2's sound
    assert "end most replies with one short, specific question" in v3  # keeps the conversation going
