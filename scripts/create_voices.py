"""Create the custom Live voices from text descriptions (run once, by the app developer).

    python scripts/create_voices.py            # all voices below
    python scripts/create_voices.py oksana     # just one

Prints each new voice id; put it into VOICE_PERSONAS (voice/options.py). Voices created from
a text prompt work only in GPT-Live — not in the legacy Realtime engine or the speech endpoint.
Each run creates NEW voices (the API has no update), so re-run only for the ones to replace.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI  # noqa: E402

from config import OPENAI_API_KEY  # noqa: E402

_NATIVE = (
    "A native Ukrainian speaker from Kyiv with clean, natural Ukrainian pronunciation and no "
    "foreign accent. Sounds like a real person in a casual phone conversation, not a narrator, "
    "announcer or audiobook reader: relaxed, conversational, natural breathing, lively intonation."
)

# key → (name, description, script the voice reads while being created)
VOICES: dict[str, tuple[str, str, str]] = {
    "oksana": (
        "Oksana",
        f"A warm, friendly woman of about 28 with a medium-pitched, slightly husky voice and an "
        f"audible smile. Kind and attentive, like a close friend helping out. {_NATIVE}",
        "Привіт! Ну що, давай подивимося, що в тебе на завтра. О, дивись — ранок вільний, а о "
        "третій зустріч із Олегом. Хочеш, я нагадаю тобі за пів години?",
    ),
    "solomiia": (
        "Solomiia",
        f"An energetic, cheerful young woman of about 22 with a bright, light, higher-pitched voice. "
        f"Speaks fairly quickly with bouncy intonation and laughs easily. {_NATIVE}",
        "Ого, та це ж уже завтра! Так-так, зараз знайду... є! Слухай, у тебе сьогодні три зустрічі "
        "поспіль, ти просто нарозхват. Може, хоч на обід час залишимо?",
    ),
    "iryna": (
        "Iryna",
        f"A calm, gentle woman of about 40 with a soft, low-medium, velvety voice. Unhurried, soothing "
        f"and reassuring, speaks a little slower than average but in a smooth, continuous flow. {_NATIVE}",
        "Добре, давай спокійно все подивимося. На вечір у тебе нічого немає, тож можна просто "
        "відпочити. А листа від бухгалтерії я відклала — повернемося до нього завтра зранку.",
    ),
    "viktoriia": (
        "Viktoriia",
        f"A confident, composed professional woman of about 33 with a clear, crisp, medium-pitched "
        f"voice. Efficient and articulate like a great executive assistant, but friendly and warm. "
        f"{_NATIVE}",
        "Так, на завтра дві зустрічі — о десятій і о пів на третю. Чернетку листа я вже створила. "
        "Перенести зустріч з Іриною на четвер чи залишаємо як є?",
    ),
    "andrii": (
        "Andrii",
        f"A calm, grounded man of about 35 with a deep, warm, relaxed baritone. Unhurried and confident, "
        f"easygoing, with a hint of dry humour. {_NATIVE}",
        "Ну, дивись. Тут є два варіанти: або переносимо зустріч на п'ятницю, або скорочуємо її до "
        "пів години. Я б обрав перше — вранці в тебе все одно вільно.",
    ),
    "taras": (
        "Taras",
        f"An upbeat, friendly young man of about 26 with a bright, clear tenor voice. Energetic and "
        f"positive, speaks at a lively pace with expressive intonation. {_NATIVE}",
        "Привіт! Слухай, я знайшов те, що ти просив. Новини такі: реліз перенесли на наступний "
        "тиждень. А в календарі на сьогодні в тебе ще тренування о сьомій — не забудь!",
    ),
}


def main() -> None:
    client = OpenAI(api_key=OPENAI_API_KEY)
    keys = sys.argv[1:] or list(VOICES)
    for key in keys:
        name, prompt, script = VOICES[key]
        voice = client.audio.voices.create(type="prompt", name=name, prompt=prompt, script_hint=script)
        print(f"{key}: {voice.id}")


if __name__ == "__main__":
    main()
