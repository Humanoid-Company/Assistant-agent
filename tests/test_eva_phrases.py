"""«Єва, скажи» / «Дякую, Єва»: tolerant to STT spelling, strict about other words."""
from __future__ import annotations

import pytest

from voice.wake_phrases import is_stop, match_wake


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("Єва, скажи", ""),
        ("єва скажи", ""),
        ("Єво, скажи, яка завтра погода?", "яка завтра погода"),
        ("Ева скажи котра година", "котра година"),
        ("Eva, скажи", ""),
        ("є ва скажи", ""),
        ("Єва, скажі", ""),  # STT typo
        ("Єва, ну скажи", ""),
        ("Скажи, Єва, що в мене завтра", "що в мене завтра"),
        ("Є́ва, кажи", ""),
    ],
)
def test_wake_variants(text, rest):
    assert match_wake(text) == rest


@pytest.mark.parametrize(
    "text",
    [
        "скажи два слова",
        "нова скажи",
        "я скажу Єві завтра",
        "Єва дуже гарне ім'я",
        "скажи",
        "",
    ],
)
def test_not_a_wake(text):
    assert match_wake(text) is None


@pytest.mark.parametrize(
    "text",
    ["Дякую, Єва", "дякую Єво", "Єва, дякую", "Дякую, Ева!", "о, дякую, Єво, все", "дякуєм Єва", "Спасибі, Єва"],
)
def test_stop_variants(text):
    assert is_stop(text)


@pytest.mark.parametrize("text", ["дякую", "дякую за допомогу", "Єва, скажи", "два дякую", ""])
def test_not_a_stop(text):
    assert not is_stop(text)
