# CALENDAR_SILENCE_AND_RECONCILE_FIX

**Проєкт:** `voice-agent-v3-2`  
**Дата:** 2026-09-29

## Verified root causes

### Silence after `confirm` → `ambiguous`

1. Лише `confirmation_required` озвучувався через `rt.say()`.
2. Для `ambiguous` / `success` / `error` / `not_found` код покладався на model follow-up після `submit_deferred_tool_result`.
3. Модель відповіла лише `note_emotion` (silent tool) без аудіо.
4. Realtime форсив follow-up (`tool_choice=auto`); якщо він теж без мови — тиша (ланцюг обмежений одним force).

Це видно в коді: `_should_speak` раніше не існував; гілка `say()` була тільки для `confirmation_required`.

### Повторний `cancel` після «Молодець»

1. `_guard_pending` блокував лише `pending` / `executing`, **не** `ambiguous`.
2. Після uncertain delete подія часто вже була видалена, але статус лишався `ambiguous` без reconcile через `get_event`.
3. Новий `cancel` шукав подію → `not_found`.

## Changes

| File | Purpose |
|---|---|
| `assistant.py` | `say()` для terminal router statuses (`success`, `ambiguous`, `not_found`, `error`, …); JSON лишається для моделі |
| `agents/calendar_agent.py` | `_reconcile_cancel` після uncertain delete; block new mutations while `ambiguous`; confirm-yes на ambiguous лише звіряє `event_id` |
| `integrations/google_calendar.py` | `fail_get_on_call` для тестів reconcile |
| `tests/test_calendar_silence_and_reconcile.py` | нові регресії |
| `tests/test_calendar_edit_delete.py` | очікування під reconcile |

## Tests

```text
uv run python -m pytest -q
→ 156 passed, 0 failed
```

(3 warnings від speech_recognition / webrtcvad — як раніше.)

## Limitations

- Create/reschedule uncertain path ще без такого ж reconcile за `event_id` (лише cancel).
- `say()` тримає фразу в `conversation: none`; статус для моделі — у function_call_output JSON.
- Живий Google / мікрофон у цьому прогоні **не** перевірялись.

## Manual voice checklist

1. «Видали сніданок» → чути підтвердження з назвою/часом.
2. «Так» → чути підсумок (успіх / все ще є / не можу підтвердити). Немає тиші після `note_emotion`.
3. «Молодець» / «Дякую» → **немає** нового cancel; якщо попереднє unresolved — пояснення, без повторного delete.
4. Успішне видалення → рівно одна відсутність події в Calendar; повторне «так» не дублює delete.
5. Create / reschedule / confirm як раніше з голосом.
