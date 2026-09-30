"""Spoken Ukrainian/Russian clock phrases must ground create schedule slots."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from agents.calendar_agent import _mentioned_times
from assistant import calendar_tool_args
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
FROZEN = datetime(2026, 9, 29, 12, 0, tzinfo=KYIV)
TODAY = "2026-09-29"
SESSION = "voice-session"


def test_mentioned_times_seventh_hour_and_nineteen_oh_oh():
    assert "19:00" in _mentioned_times("А можна на сьогодні записати на сьому годину?")
    assert "07:00" in _mentioned_times("на сьому годину")
    assert "19:00" in _mentioned_times("Девятнадцата нуль нуль.")
    assert "19:00" in _mentioned_times("дев'ятнадцять нуль нуль")
    assert "20:00" in _mentioned_times("на 8 вечора")


def test_create_from_syomu_hodynu_then_title(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar._now_source = lambda: FROZEN
    first = router.calendar_action(
        **calendar_tool_args(
            {"action": "create", "title": "Подія", "date": "сьогодні", "time": "19:00"},
            session_id=SESSION,
            user_utterances=["А можна на сьогодні записати на сьому годину? У мене зустріч має бути."],
        )
    )
    assert first.status == "needs_more_info"
    assert first.data["reason_code"] == "title_not_from_user"

    named = router.calendar_action(
        **calendar_tool_args(
            {"action": "create", "title": "Зустріч з другом", "date": TODAY, "time": "19:00"},
            session_id=SESSION,
            user_utterances=[
                "А можна на сьогодні записати на сьому годину? У мене зустріч має бути.",
                "Може зустріч з другом?",
            ],
        )
    )
    assert named.status == "confirmation_required", named
    assert "19:00" in named.message
    assert "сьогодні" in named.message
    assert TODAY not in named.message
    assert cal.create_calls == 0


def test_spoken_nineteen_oh_oh_accepts_time(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar._now_source = lambda: FROZEN
    router.calendar_action(
        **calendar_tool_args(
            {"action": "create", "title": "X", "date": "сьогодні", "time": "19:00"},
            session_id=SESSION,
            user_utterances=["сьогодні зустріч"],
        )
    )
    # Time was missing from speech — now user speaks it in words.
    named = router.calendar_action(
        **calendar_tool_args(
            {"action": "create", "title": "Зустріч з другом", "date": TODAY, "time": "19:00"},
            session_id=SESSION,
            user_utterances=[
                "сьогодні зустріч",
                "Зустріч з другом",
                "Девятнадцата нуль нуль.",
            ],
        )
    )
    assert named.status == "confirmation_required", named
    assert "19:00" in named.message
    assert cal.create_calls == 0
