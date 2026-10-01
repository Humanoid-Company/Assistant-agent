"""Spoken Ukrainian/Russian time phrases, deictic references and duration wording."""
from __future__ import annotations

import re
from datetime import datetime

from agents.create_draft import (
    is_acknowledgement,
)
from integrations.google_calendar import (
    speak_date,
)

_CHOICE_TITLE_RE = re.compile(r"\b(або|чи|or)\b", re.IGNORECASE)
_HOUR_RE = re.compile(r"(?:о|в|у|на)\s+(\d{1,2})(?:[:.](\d{2}))?(?!\d)", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)")
_EVENING_RE = re.compile(
    r"(?:о|в|у|на)?\s*(\d{1,2})(?:[:.](\d{2}))?\s*(вечора|вечір|вечера|вечером|дня|днем|ранку|зранку|ночі|ночью)",
    re.IGNORECASE,
)
# Spoken hour words (uk/ru cardinals + common ordinals used in "на сьому годину").
_HOUR_WORDS: dict[str, int] = {
    "нуль": 0,
    "один": 1,
    "одна": 1,
    "першу": 1,
    "перша": 1,
    "первой": 1,
    "два": 2,
    "дві": 2,
    "другу": 2,
    "друга": 2,
    "второй": 2,
    "три": 3,
    "третю": 3,
    "третя": 3,
    "третью": 3,
    "чотири": 4,
    "четверту": 4,
    "четверта": 4,
    "четвертую": 4,
    "пять": 5,
    "п'ять": 5,
    "пят": 5,
    "пʼять": 5,
    "пʼяту": 5,
    "п'яту": 5,
    "пятую": 5,
    "шість": 6,
    "шесть": 6,
    "шосту": 6,
    "шоста": 6,
    "шестую": 6,
    "сім": 7,
    "семь": 7,
    "сьому": 7,
    "сьома": 7,
    "седьмую": 7,
    "седьмая": 7,
    "восемь": 8,
    "вісім": 8,
    "восьму": 8,
    "восьма": 8,
    "восьмую": 8,
    "дев'ять": 9,
    "девять": 9,
    "девʼять": 9,
    "дев'яту": 9,
    "девʼяту": 9,
    "девятую": 9,
    "десять": 10,
    "десяту": 10,
    "десятая": 10,
    "десятую": 10,
    "одинадцять": 11,
    "одиннадцать": 11,
    "одинадцяту": 11,
    "одиннадцатую": 11,
    "дванадцять": 12,
    "двенадцать": 12,
    "дванадцяту": 12,
    "двенадцатую": 12,
    "тринадцять": 13,
    "тринадцать": 13,
    "тринадцяту": 13,
    "тринадцатую": 13,
    "чотирнадцять": 14,
    "четырнадцать": 14,
    "чотирнадцяту": 14,
    "четырнадцатую": 14,
    "п'ятнадцять": 15,
    "пятнадцать": 15,
    "пʼятнадцять": 15,
    "п'ятнадцяту": 15,
    "пʼятнадцяту": 15,
    "пятнадцатую": 15,
    "шістнадцять": 16,
    "шестнадцать": 16,
    "шістнадцяту": 16,
    "шестнадцатую": 16,
    "сімнадцять": 17,
    "семнадцать": 17,
    "сімнадцяту": 17,
    "семнадцатую": 17,
    "вісімнадцять": 18,
    "восемнадцать": 18,
    "вісімнадцяту": 18,
    "восемнадцатую": 18,
    "дев'ятнадцять": 19,
    "девятнадцать": 19,
    "девʼятнадцять": 19,
    "дев'ятнадцяту": 19,
    "девʼятнадцяту": 19,
    "девятнадцатую": 19,
    "девятнадцата": 19,
    "девятнадцатый": 19,
    "двадцять": 20,
    "двадцать": 20,
    "двадцяту": 20,
    "двадцатую": 20,
    "двадцять одну": 21,
    "двадцать один": 21,
    "двадцять дві": 22,
    "двадцать два": 22,
    "двадцять три": 23,
    "двадцать три": 23,
}
_WORD_HOUR_RE = re.compile(
    r"(?:о|в|у|на|о\s*коло)?\s*"
    r"(нуль|один|одна|перш\w*|два|дві|друг\w*|три|трет\w*|чотири|четверт\w*|"
    r"п['ʼ]?ят\w*|пять|пят\w*|шість|шесть|шост\w*|шест\w*|сім|семь|сьом\w*|седьм\w*|"
    r"вісім|восемь|восьм\w*|дев['ʼ]?ят\w*|десят\w*|одинадцят\w*|одиннадцат\w*|"
    r"дванадцят\w*|двенадцат\w*|тринадцят\w*|тринадцат\w*|чотирнадцят\w*|четырнадцат\w*|"
    r"п['ʼ]?ятнадцят\w*|пятнадцат\w*|шістнадцят\w*|шестнадцат\w*|сімнадцят\w*|семнадцат\w*|"
    r"вісімнадцят\w*|восемнадцат\w*|дев['ʼ]?ятнадцят\w*|девятнадцат\w*|"
    r"двадцят\w*|двадцат\w*(?:\s+(?:один|одна|два|дві|три))?)"
    r"(?:\s+(?:нуль|ноль)\s+(?:нуль|ноль))?"
    r"(?:\s*(?:годин\w*|час\w*|часа))?"
    r"(?:\s*(вечора|вечір|вечера|вечером|дня|днем|ранку|зранку|ночі|ночью))?",
    re.IGNORECASE,
)
_SPOKEN_HHMM_RE = re.compile(
    r"\b(\d{1,2})\s+(?:нуль|ноль|00)\s+(?:нуль|ноль|00)\b",
    re.IGNORECASE,
)


def _hour_period(hour: int, period: str | None) -> int:
    if not period:
        return hour
    label = period.casefold()
    if "ранк" in label or "зранк" in label:
        return hour % 12
    if "веч" in label or "дня" in label or "днем" in label:
        return hour if 12 <= hour <= 23 else (hour + 12 if hour < 12 else hour)
    if "ноч" in label:
        return 0 if hour == 12 else hour % 12
    return hour


def _lookup_hour_word(token: str) -> int | None:
    key = (
        token.casefold()
        .replace("ʼ", "'")
        .replace("`", "'")
        .replace("’", "'")
        .strip()
    )
    key = re.sub(r"\s+", " ", key)
    if key in _HOUR_WORDS:
        return _HOUR_WORDS[key]
    stems = (
        ("девятнадцат", 19),
        ("дев'ятнадцят", 19),
        ("семнадцат", 17),
        ("сімнадцят", 17),
        ("восемнадцат", 18),
        ("вісімнадцят", 18),
        ("шестнадцат", 16),
        ("шістнадцят", 16),
        ("пятнадцат", 15),
        ("п'ятнадцят", 15),
        ("четырнадцат", 14),
        ("чотирнадцят", 14),
        ("тринадцат", 13),
        ("тринадцят", 13),
        ("двенадцат", 12),
        ("дванадцят", 12),
        ("одиннадцат", 11),
        ("одинадцят", 11),
        ("двадцать три", 23),
        ("двадцять три", 23),
        ("двадцать два", 22),
        ("двадцять дві", 22),
        ("двадцать один", 21),
        ("двадцять од", 21),
        ("двадцат", 20),
        ("двадцят", 20),
        ("десят", 10),
        ("дев'ят", 9),
        ("девять", 9),
        ("девятую", 9),
        ("восьм", 8),
        ("сьом", 7),
        ("седьм", 7),
        ("шост", 6),
        ("шест", 6),
        ("п'ят", 5),
        ("пят", 5),
        ("четверт", 4),
        ("трет", 3),
        ("друг", 2),
        ("перш", 1),
        ("перв", 1),
    )
    for stem, value in stems:
        if key.startswith(stem):
            return value
    return None


def _mentioned_times(text: str) -> set[str]:
    found: set[str] = set()
    for match in _CLOCK_RE.finditer(text):
        hour = int(match.group(1))
        if hour <= 23:
            found.add(f"{hour:02d}:{match.group(2)}")
    for match in _EVENING_RE.finditer(text):
        hour = _hour_period(int(match.group(1)), match.group(3))
        minute = match.group(2) or "00"
        if hour <= 23:
            found.add(f"{hour:02d}:{minute}")
    for match in _HOUR_RE.finditer(text):
        hour = int(match.group(1))
        minute = match.group(2) or "00"
        if hour <= 23:
            found.add(f"{hour:02d}:{minute}")
            if 1 <= hour <= 11:
                found.add(f"{hour + 12:02d}:{minute}")
    for match in _SPOKEN_HHMM_RE.finditer(text):
        hour = int(match.group(1))
        if hour <= 23:
            found.add(f"{hour:02d}:00")
    for match in _WORD_HOUR_RE.finditer(text):
        hour = _lookup_hour_word(match.group(1))
        if hour is None:
            continue
        period = match.group(2)
        hour = _hour_period(hour, period)
        if hour > 23:
            continue
        found.add(f"{hour:02d}:00")
        if period is None and 1 <= hour <= 11:
            found.add(f"{hour + 12:02d}:00")
    return found


def _is_acknowledgement(text: str) -> bool:
    return is_acknowledgement(text)


_DEICTIC_RE = re.compile(
    r"^(цю(\s+подію|\s+зустріч)?|ця(\s+подія)?|її|його|цю\s+саму|"
    r"(цю\s+)?подію,?\s+яку\s+щойно\s+знайшли|щойно\s+знайден\w*|"
    r"останн\w+(\s+подію|\s+зустріч)?)$",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"(\d+)\s*хвилин", re.IGNORECASE)


def _is_deictic(text: str | None) -> bool:
    if not text or not str(text).strip():
        return False
    cleaned = re.sub(
        r"\b(перенеси|перенести|зміни|змінити|скасуй|видали|видалити|подію|подія|зустріч)\b",
        " ",
        str(text).lower(),
    )
    cleaned = re.sub(r"[^\w\s'’ʼ-]+", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    if "щойно знайш" in cleaned:
        return True
    return _DEICTIC_RE.match(cleaned) is not None


def _duration_phrase(minutes: int) -> str:
    if minutes <= 0:
        return f"{minutes} хвилин"
    if minutes % 60 == 0:
        hours = minutes // 60
        if hours == 1:
            return "1 година"
        if 2 <= hours <= 4:
            return f"{hours} години"
        return f"{hours} годин"
    return f"{minutes} хвилин"


def _day_phrase(day: str, now: datetime) -> str:
    return speak_date(day, now=now)
