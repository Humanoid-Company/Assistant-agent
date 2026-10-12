"""Other calendars switched on in Google Calendar (a university timetable) are read with the primary
one, named in the answer and never changed; a leftover word («по планах») never hides a whole week."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from assistant import calendar_tool_args
from integrations.google_calendar import GoogleCalendarClient
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 12, 11, 58, tzinfo=KYIV)  # Monday
UNI = "uni-3rd-year@group.calendar.google.com"


def _event(event_id: str, summary: str, day: str, hm: str, minutes: int = 80) -> dict:
    start = datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=KYIV)
    return {
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
    cal.events["client"] = _event("client", "Зустріч з замовником", "2026-10-13", "12:00", 15)
    cal.other_calendars = [(UNI, "IT УНІ, бакалаврат, 3 курс", [
        _event("bigdata", "4SD-31 - Big Data, лек.", "2026-10-13", "12:10"),
        _event("ux", "4DD-31 - UI/UX Design, пр.", "2026-10-13", "14:00"),
    ])]
    return router, cal


def _call(router, **args):
    return router.calendar_action(**calendar_tool_args(args, session_id="s"))


def test_week_lists_every_calendar_and_names_the_other_one(setup):
    router, _cal = setup
    result = _call(router, action="list", query="що в мене на цей тиждень")
    events = result.data["events"]
    assert [e["event_id"] for e in events] == ["client", "bigdata", "ux"]
    assert events[0]["calendar_id"] == "primary" and "read_only" not in events[0]
    assert events[1]["calendar_id"] == UNI and events[1]["read_only"] is True
    assert "Big Data, лек. (календар «IT УНІ, бакалаврат, 3 курс»)" in result.message


def test_another_calendars_event_is_never_the_one_to_change(setup):
    router, _cal = setup
    found = _call(router, action="search", query="Big Data")
    assert [e["event_id"] for e in found.data["events"]] == ["bigdata"]
    assert not router.calendar._selected  # «перенеси її» has nothing to act on


def test_leftover_words_never_hide_the_week(setup):
    router, _cal = setup
    result = _call(router, action="list", query="знаєш по календарю щось, що в мене на цей тиждень по планах")
    assert {e["event_id"] for e in result.data["events"]} == {"client", "bigdata", "ux"}


def test_a_name_that_matches_nothing_still_gets_the_period(setup):
    router, _cal = setup
    result = _call(router, action="list", query="йога на цей тиждень")
    assert result.message.startswith("Подій із назвою «йога» немає. ")
    assert len(result.data["events"]) == 3


def test_a_busy_week_is_counted():
    from agents import calendar_agent

    agent = SimpleNamespace(_spoken_line=lambda e: e["summary"], _public_event=lambda e: e, _forget=lambda *a: None)
    events = [{"summary": f"Пара {i}"} for i in range(30)]
    result = calendar_agent.CalendarAgent._events_result(agent, "sub", "s", events, search_mode=False)
    assert "і ще 22 (усього 30)" in result.message and len(result.data["events"]) == 20


def test_google_client_reads_only_calendars_switched_on():
    calls: list[str] = []
    calendar_list = {"items": [
        {"id": "me@gmail.com", "primary": True, "selected": True, "accessRole": "owner"},
        {"id": UNI, "summary": "IT УНІ", "selected": True, "accessRole": "reader"},
        {"id": "off", "summary": "Вимкнений", "selected": False, "accessRole": "reader"},
        {"id": "busy", "summary": "Лише зайнятість", "selected": True, "accessRole": "freeBusyReader"},
        {"id": "hidden", "summary": "Прихований", "selected": True, "hidden": True, "accessRole": "owner"},
    ]}

    def events_list(**kw):
        calls.append(kw["calendarId"])
        day = "2026-10-13"
        return {"items": [_event(f"e-{kw['calendarId']}", kw["calendarId"], day, "10:00" if kw["calendarId"] == UNI else "09:00")]}

    service = SimpleNamespace(
        calendarList=lambda: SimpleNamespace(list=lambda **kw: calendar_list),
        events=lambda: SimpleNamespace(list=events_list),
    )
    client = GoogleCalendarClient.__new__(GoogleCalendarClient)
    client._service, client._calendar_id = service, "primary"
    client._read = lambda request: request
    found = client.list_all_events(NOW, NOW + timedelta(days=7))
    assert calls == ["primary", UNI]
    assert [e.get("_calendar_name") for e in found] == [None, "IT УНІ"]
