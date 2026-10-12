"""«Що в мене на тиждень?» with several calendars: she asks — all of them or one — and keeps the answer
for a while. Reminders: «і нагадай за годину» on a new event, «постав нагадування за дві години до
зустрічі» on an existing one; a phone notification, confirmed like any change."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agents import calendar_agent
from agents.calendar_speech import _reminder_phrase
from assistant import calendar_tool_args
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=KYIV)  # Monday
DAY = "2026-10-13"
UNI = "uni@group.calendar.google.com"


def _event(event_id: str, summary: str, hm: str, minutes: int = 60) -> dict:
    start = datetime.fromisoformat(f"{DAY}T{hm}:00").replace(tzinfo=KYIV)
    return {
        "id": event_id,
        "summary": summary,
        "etag": "e1",
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Kyiv"},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(), "timeZone": "Europe/Kyiv"},
    }


@pytest.fixture()
def setup(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar._now_source = lambda: NOW
    cal.events["client"] = _event("client", "Зустріч з замовниками", "11:50", 30)
    cal.other_calendars = [(UNI, "IT УНІ, бакалаврат", [_event("bigdata", "Big Data", "12:10", 80)])]
    return router, cal


def _call(router, said: list[str] | None = None, **args):
    return router.calendar_action(**calendar_tool_args(args, session_id="s", user_utterances=said))


def _ids(result) -> set[str]:
    return {e["event_id"] for e in (result.data or {}).get("events", [])}


# ── which calendars ──────────────────────────────────────────────────────────

def test_a_listing_asks_which_calendars(setup):
    router, _cal = setup
    asked = _call(router, action="list", query="що в мене на цей тиждень")
    assert asked.status == "needs_more_info" and asked.data["reason_code"] == "calendar_choice"
    assert asked.message == "Подивитись в усіх календарях чи в якомусь одному? Є: основний, «IT УНІ, бакалаврат»."


@pytest.mark.parametrize(
    ("choice", "ids"),
    [
        ("в усіх", {"client", "bigdata"}),
        ("all", {"client", "bigdata"}),
        ("особистий", {"client"}),
        ("основний", {"client"}),
        ("primary", {"client"}),
        ("університетський", {"bigdata"}),
        ("IT УНІ, бакалаврат", {"bigdata"}),
    ],
)
def test_the_answer_picks_the_calendars(setup, choice, ids):
    router, _cal = setup
    assert _ids(_call(router, action="list", query="що в мене на цей тиждень", calendar=choice)) == ids


def test_the_choice_holds_for_the_next_listings(setup, monkeypatch):
    router, _cal = setup
    _call(router, action="list", query="на цей тиждень", calendar="особистий")
    assert _ids(_call(router, action="list", query="а завтра")) == {"client"}  # not asked again
    monkeypatch.setattr(calendar_agent, "_CHOICE_TTL_S", -1)
    _call(router, action="list", query="на цей тиждень", calendar="особистий")
    assert _call(router, action="list", query="а завтра").data["reason_code"] == "calendar_choice"


def test_an_unknown_calendar_is_asked_about(setup):
    router, _cal = setup
    result = _call(router, action="list", query="на тиждень", calendar="робочий")
    assert result.data["reason_code"] == "calendar_not_found"
    assert "Не знайшла календар «робочий»" in result.message and "«IT УНІ, бакалаврат»" in result.message


def test_one_calendar_or_a_search_never_asks(setup):
    router, cal = setup
    assert _ids(_call(router, action="search", query="Big Data")) == {"bigdata"}
    cal.other_calendars = []
    assert _ids(_call(router, action="list", query="на цей тиждень")) == {"client"}


# ── reminders ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("minutes", "phrase"),
    [(0, "на початку"), (10, "за 10 хвилин"), (60, "за годину"), (90, "за півтори години"),
     (120, "за 2 години"), (300, "за 5 годин"), (1440, "за день"), (2880, "за 2 дні")],
)
def test_reminder_phrases(minutes, phrase):
    assert _reminder_phrase(minutes) == phrase


def test_new_event_with_a_reminder(setup):
    router, cal = setup
    said = ["Створи зустріч з ментором завтра о 15:00 і нагадай за годину"]
    preview = _call(router, said, action="create", title="Зустріч з ментором", date=DAY, time="15:00",
                    duration_minutes=60, reminder_minutes=60)
    assert preview.status == "confirmation_required"
    assert "на завтра о 15:00 з нагадуванням за годину. Підтвердити?" in preview.message
    with_meet = _call(router, said, action="create", title="Зустріч з ментором", date=DAY, time="15:00",
                      duration_minutes=60, reminder_minutes=60, with_meet=True)
    assert "з Google Meet і нагадуванням за годину" in with_meet.message
    # the second preview replaced the first: that is the one to confirm
    done = _call(router, ["так"], action="confirm", confirmation="так", op_id=with_meet.data["op_id"])
    assert done.status == "success" and "Нагадування — за годину." in done.message
    created = next(e for e in cal.events.values() if e.get("summary") == "Зустріч з ментором")
    assert created["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]}


def test_reminder_for_an_existing_event(setup):
    router, cal = setup
    said = ["Постав нагадування за дві години до зустрічі з замовниками"]
    preview = _call(router, said, action="update", title="Зустріч з замовниками", date=DAY, reminder_minutes=120)
    assert preview.status == "confirmation_required"
    assert "Поставити нагадування за 2 години до «Зустріч з замовниками»" in preview.message
    done = _call(router, ["так"], action="confirm", confirmation="так", op_id=preview.data["op_id"])
    assert done.status == "success"
    assert cal.events["client"]["reminders"]["overrides"] == [{"method": "popup", "minutes": 120}]
    assert cal.events["client"]["start"]["dateTime"].startswith(f"{DAY}T11:50")  # not moved


@pytest.mark.parametrize("minutes", [-5, 50000])
def test_reminder_out_of_range_is_refused(setup, minutes):
    router, _cal = setup
    result = _call(router, action="update", title="Зустріч з замовниками", date=DAY, reminder_minutes=minutes)
    assert result.status == "needs_more_info" and "reminder_minutes" in str(result.data)
