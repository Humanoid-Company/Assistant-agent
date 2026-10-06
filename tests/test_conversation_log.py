"""ConversationLog: the dialogue history that outlives a Live session (and its voice)."""
from __future__ import annotations

from voice.conversation import ConversationLog


def test_deltas_group_into_turns_by_speaker():
    log = ConversationLog()
    for role, delta in [("user", "При"), ("user", "віт"), ("assistant", "Привіт!"), ("user", "Як справи?")]:
        log.add(role, delta)
    assert [(t.role, t.text) for t in log.turns()] == [
        ("user", "Привіт"),
        ("assistant", "Привіт!"),
        ("user", "Як справи?"),
    ]


def test_live_input_keeps_the_newest_turns_within_limits():
    log = ConversationLog()
    for i in range(10):
        log.add("user", f"питання {i}")
        log.add("assistant", f"відповідь {i}")
    items = log.live_input(max_messages=4)
    assert [i["content"][0]["text"] for i in items] == ["питання 8", "відповідь 8", "питання 9", "відповідь 9"]
    assert items[0]["content"][0]["type"] == "input_text"
    assert items[1]["content"][0]["type"] == "output_text"
    assert log.live_input(max_chars=12)[-1]["content"][0]["text"] == "відповідь 9"


def test_end_turn_splits_same_speaker():
    log = ConversationLog()
    log.add("user", "перше")
    log.end_turn()
    log.add("user", "друге")
    assert [t.text for t in log.turns()] == ["перше", "друге"]
