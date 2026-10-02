"""Dates said in words count as said by the user — otherwise create loops on «на яку дату?»."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agents.calendar_speech import _mentioned_dates
from assistant import calendar_tool_args
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=KYIV)  # Friday


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Запиши зустріч 13 жовтня на другу годину", "2026-10-13"),
        ("тринадцяте жовтня", "2026-10-13"),
        ("Три тринадцяте жовтня дві тисячі двадцять шостого", "2026-10-13"),
        ("Дванадцятого десятого двадцять шостого", "2026-10-12"),
        ("зустріч на дванадцяте жовтня о дев'ятнадцятій", "2026-10-12"),
        ("двадцять п'ятого жовтня", "2026-10-25"),
        ("тридцять першого грудня", "2026-12-31"),
        ("13-го жовтня 2026 року", "2026-10-13"),
        ("на 13.10", "2026-10-13"),
        ("першого вересня", "2027-09-01"),  # already past this year → next year
        ("12 жовтня тридцятого року", "2030-10-12"),
        ("в понеділок", "2026-10-05"),
        ("в п'ятницю", "2026-10-09"),
    ],
)
def test_spoken_date_is_recognized(text, expected):
    assert expected in _mentioned_dates(text, NOW)


@pytest.mark.parametrize(
    "text",
    ["зустріч з другом о другій", "на годину", "Так", "о 14.30", "четверте питання"],
)
def test_no_date_without_a_month(text):
    assert _mentioned_dates(text, NOW) == set()


def test_create_from_spoken_date_needs_no_repeats(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar._now_source = lambda: NOW
    said = [
        "Запиши зустріч. Мені 13 вересня. Ні, 13 жовтня. На другу годину, "
        "назви її зустріч з замовником і дай туди Google Meet",
    ]
    preview = router.calendar_action(
        **calendar_tool_args(
            {
                "action": "create",
                "title": "Зустріч з замовником",
                "date": "2026-10-13",
                "time": "14:00",
                "with_meet": True,
            },
            session_id="s",
            user_utterances=said,
        )
    )
    assert preview.status == "confirmation_required", preview.message
    assert cal.create_calls == 0
