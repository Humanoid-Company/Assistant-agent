"""«Які в мене завтра плани?» must list tomorrow — question words are not an event-name filter."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant import calendar_tool_args
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 2, 16, 0, tzinfo=KYIV)  # Friday
TOMORROW = "2026-10-03"


def _seed(cal, event_id: str, summary: str, day: str, hm: str, minutes: int = 60) -> None:
    start = datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=KYIV)
    cal.events[event_id] = {
        "id": event_id,
        "summary": summary,
        "etag": "e",
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Kyiv"},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(), "timeZone": "Europe/Kyiv"},
    }


@pytest.fixture()
def setup(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar._now_source = lambda: NOW
    _seed(cal, "a", "Співбесіда в Humanoid Company", TOMORROW, "15:00", 90)
    _seed(cal, "b", "Зустріч", TOMORROW, "15:00")
    _seed(cal, "c", "Обід", TOMORROW, "15:00")
    _seed(cal, "d", "Погуляти", TOMORROW, "17:00")
    _seed(cal, "mon", "Планірка", "2026-10-05", "10:00")
    _seed(cal, "nextweek", "Ретро", "2026-10-13", "12:00")
    return router, cal


def _ids(result) -> set[str]:
    return {e["event_id"] for e in (result.data or {}).get("events", [])}


def _list(router, query=None, date=None):
    args = {"action": "list"}
    if query:
        args["query"] = query
    if date:
        args["date"] = date
    return router.calendar_action(**calendar_tool_args(args, session_id="s"))


@pytest.mark.parametrize(
    "query",
    [
        "які в мене завтра плани",
        "що в мене завтра",
        "плани на завтра",
        "що там на завтра о третій",
        "на 3 жовтня",
        "на суботу",
        "глянь що в мене в календарі на завтра",
    ],
)
def test_question_about_a_day_lists_the_whole_day(setup, query):
    router, _cal = setup
    assert _ids(_list(router, query)) == {"a", "b", "c", "d"}


def test_date_in_words_from_the_model(setup):
    router, _cal = setup
    assert _ids(_list(router, date="3 жовтня")) == {"a", "b", "c", "d"}
    assert _ids(_list(router, date="завтра")) == {"a", "b", "c", "d"}


def test_week_ranges(setup):
    router, _cal = setup
    assert _ids(_list(router, "що в мене на цей тиждень")) == {"a", "b", "c", "d"}
    assert _ids(_list(router, "а наступного тижня")) == {"mon"}  # Mon 5 … Sun 11; «Ретро» on the 13th is later
    assert _ids(_list(router, "що на вихідні")) == {"a", "b", "c", "d"}


def test_a_real_title_still_filters(setup):
    router, _cal = setup
    assert _ids(_list(router, "обід завтра")) == {"c"}
    assert _ids(_list(router, "коли в мене погуляти")) == {"d"}
