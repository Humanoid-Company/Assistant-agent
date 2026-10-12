"""A taken slot, then a new time: «після всіх пар» → she offers 19:50 → «Так». The new plan replaces
the unanswered preview, the time she offered counts once the user agrees, and an unsaid time is
never silently swapped for one said earlier («система знову підставила тринадцяту»)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agents.calendar_speech import _mentioned_times
from assistant import calendar_tool_args
from tests.helpers_google import build_test_router

KYIV = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 12, 12, 30, tzinfo=KYIV)
DAY = "2026-10-13"
ASKED = [
    "запиши мені завтра подію на 13",
    "Давай подію, хай буде зустріч з ментором на 13-ту на годину. І додай Google Meet туди",
]


def _class(event_id: str, summary: str, hm: str, minutes: int) -> dict:
    start = datetime.fromisoformat(f"{DAY}T{hm}:00").replace(tzinfo=KYIV)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat()},
    }


@pytest.fixture()
def router(tmp_path):
    router, cal, *_ = build_test_router(tmp_path)
    router.calendar._now_source = lambda: NOW
    cal.other_calendars = [("uni", "IT УНІ", [
        _class("bd", "Big Data", "12:10", 80),
        _class("cons", "Консультація", "18:30", 80),
    ])]
    return router


def _create(router, hm: str, said: list[str], her: list[str] | None = None):
    args = {
        "action": "create", "title": "Зустріч з ментором", "date": DAY, "time": hm,
        "duration_minutes": 60, "with_meet": True,
    }
    return router.calendar_action(
        **calendar_tool_args(args, session_id="s", user_utterances=said, assistant_utterances=her)
    )


def test_offered_time_after_a_taken_slot_is_created(router):
    first = _create(router, "13:00", ASKED)
    assert first.status == "confirmation_required" and "Big Data" in first.message

    said = ASKED + ["Після всіх пар завтра", "Так"]
    her = ["Ой, вона закінчується о дев’ятнадцятій п’ятдесят. Поставити зустріч на дев’ятнадцять п’ятдесят?"]
    second = _create(router, "19:50", said, her)
    assert second.status == "confirmation_required"
    assert "19:50" in second.message and "13:00" not in second.message
    assert second.data["op_id"] != first.data["op_id"]  # the new plan replaced the old question


def test_a_time_nobody_said_is_asked_not_swapped(router):
    _create(router, "13:00", ASKED)
    result = _create(router, "19:50", ASKED + ["Після всіх пар завтра"])
    assert result.status == "needs_more_info"
    assert result.data["reason_code"] == "schedule_not_from_user"
    assert "13:00" not in result.message


def test_no_is_not_agreement_to_her_offer(router):
    her = ["Поставити зустріч на дев’ятнадцять п’ятдесят?"]
    result = _create(router, "19:50", ASKED + ["Та ні"], her)
    assert result.status == "needs_more_info"


@pytest.mark.parametrize(
    ("said", "times"),
    [
        ("на дев’ятнадцять п’ятдесят", {"19:50"}),
        ("о пів на восьму вечора", {"19:30"}),
        ("о пів на третю", {"02:30", "14:30"}),
        ("без чверті три", {"02:45", "14:45"}),
        ("о сьомій тридцять вечора", {"19:30"}),
        ("о дев’ятій двадцять п’ять", {"09:25", "21:25"}),
        ("о п’ятнадцятій нуль нуль", {"15:00"}),
        ("о двадцять першій", {"21:00"}),
    ],
)
def test_spoken_minutes_and_half_hours(said, times):
    assert _mentioned_times(said) == times


def test_a_listing_says_when_events_end(router):
    result = router.calendar_action(**calendar_tool_args({"action": "list", "date": DAY, "calendar": "all"}, session_id="s"))
    assert "о 18:30 до 19:50 — Консультація" in result.message


def test_live_bridge_hands_her_last_lines_to_the_tools():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.live_bridge import SidebandToolBridge
    from tools.results import ToolResult

    executor = SimpleNamespace(execute=AsyncMock(return_value=ToolResult(ok=True, status="success", message="ok")))
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    bridge._connection = SimpleNamespace(
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
    )

    async def run():
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": "Поставити на 19:50?"})
        await bridge._handle_event({"type": "session.input_transcript.delta", "delta": "Так"})
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": "Добре, роблю."})
        await bridge._execute_and_continue(name="calendar_prepare_create", arguments="{}", call_id="c1",
                                           delegation_id=None, key="k")

    asyncio.run(run())
    ctx = executor.execute.await_args.args[2]
    assert ctx.assistant_utterances == ["Поставити на 19:50?", "Добре, роблю."]
    assert ctx.user_utterances == ["Так"]
