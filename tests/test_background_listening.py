"""Paused, not switched off: what is said nearby during a pause reaches Єва on wake."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server.app as web
from voice.background import MAX_NOTE_CHARS, BackgroundLog, wake_commentary
from voice.conversation import ConversationLog

BOB = "bob-browser-0123456789ab"
H = {"X-Client-Id": BOB}


def test_digest_returns_phrases_and_clears():
    log = BackgroundLog()
    log.add("нараду перенесли на четвер")
    log.add("ну")  # a lone noise word is not kept
    log.add("в Олега в п'ятницю день народження")
    assert log.digest() == "нараду перенесли на четвер в Олега в п'ятницю день народження"
    assert log.digest() == ""
    assert not log


def test_long_pause_is_compressed_to_one_commentary():
    calls: list[str] = []

    def summarize(text: str) -> str:
        calls.append(text)
        return "Нарада в четвер о 14:00; Марек приїде 20 жовтня."

    log = BackgroundLog(summarize)
    for i in range(80):  # well past the raw limit: folded into the running summary on the way
        log.add(f"фраза номер {i} про щось зовсім неважливе")
    note = log.digest()
    assert note == "Нарада в четвер о 14:00; Марек приїде 20 жовтня."
    assert len(calls) >= 2  # folded at least once, then compressed on digest


def test_without_summarizer_the_latest_talk_is_kept():
    log = BackgroundLog()
    for i in range(60):
        log.add(f"репліка {i} без жодного сенсу взагалі")
    note = log.digest()
    assert len(note) <= MAX_NOTE_CHARS
    assert note.endswith("репліка 59 без жодного сенсу взагалі")


def test_failing_summarizer_does_not_lose_the_pause():
    def boom(text: str) -> str:
        raise RuntimeError("offline")

    log = BackgroundLog(boom)
    for i in range(60):
        log.add(f"репліка {i} без жодного сенсу взагалі")
    assert log.digest().endswith("репліка 59 без жодного сенсу взагалі")


def test_commentary_fits_the_500_token_limit():
    # Measured: Cyrillic ≈ 2.7 chars/token, English ≈ 4. Note + wrapper + wake greeting ≤ 500.
    text = wake_commentary("ж" * MAX_NOTE_CHARS)
    wrapper = len(text) - MAX_NOTE_CHARS
    greeting = 420  # WAKE_GREETING in voice/live_driver.py, chars
    assert MAX_NOTE_CHARS / 2.7 + (wrapper + greeting) / 4 < 470


def test_note_stays_in_history_as_her_own_remark():
    log = ConversationLog()
    log.add("user", "Привіт")
    log.add_note("нарада в четвер")
    log.add("assistant", "Слухаю")
    items = log.live_input()
    assert [i["role"] for i in items] == ["user", "assistant", "assistant"]
    assert "нарада в четвер" in items[1]["content"][0]["text"]
    assert items[2]["content"][0]["text"] == "Слухаю"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    return TestClient(web.app)


def test_web_overheard_reaches_her_on_wake(client):
    user = web.users.get(BOB)
    user.conversation.clear()
    assert client.post("/api/overheard/digest", headers=H).json() == {"commentary": ""}
    client.post("/api/overheard", headers=H, json={"text": "нараду з бухгалтерією перенесли на четвер"})
    commentary = client.post("/api/overheard/digest", headers=H).json()["commentary"]
    assert "нараду з бухгалтерією перенесли на четвер" in commentary
    assert "not by the user" in commentary or "not said by the user" in commentary
    assert "нараду" in user.conversation.turns()[-1].text  # a later session still has it
    assert client.post("/api/overheard/digest", headers=H).json() == {"commentary": ""}


def test_new_conversation_forgets_the_background(client):
    client.post("/api/overheard", headers=H, json={"text": "Марек приїде двадцятого жовтня"})
    client.delete("/api/conversation", headers=H)
    assert client.post("/api/overheard/digest", headers=H).json() == {"commentary": ""}


def test_turning_background_listening_off_forgets_what_was_heard(client):
    client.post("/api/overheard", headers=H, json={"text": "Марек приїде двадцятого жовтня"})
    assert client.delete("/api/overheard", headers=H).json() == {"ok": True}
    assert client.post("/api/overheard/digest", headers=H).json() == {"commentary": ""}


def test_browser_may_send_delete_cross_origin(client):
    """The page lives on Vercel, the API on Render: DELETE needs CORS (it was GET/POST only, so
    «Нова розмова» silently never cleared the server's history)."""
    res = client.options(
        "/api/overheard",
        headers={
            "Origin": "https://voice-agents-web-three.vercel.app",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "x-client-id,content-type",
        },
    )
    assert res.status_code == 200
    assert "DELETE" in res.headers["access-control-allow-methods"]
