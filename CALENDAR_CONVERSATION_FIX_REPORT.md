# CALENDAR_CONVERSATION_FIX_REPORT

**Проєкт:** `voice-agent-v3-2`  
**Дата:** 2026-09-29  
**Тема:** багатокрокова розмова створення події, збереження параметрів, озвучення `confirmation_required`

---

## 1. Точні причини помилок

### 1.1. Повторні `title_not_from_user` після назви

`_title_said_by_user` робив лише точний `casefold`-підрядок. Модель нормалізувала відмінок («Вечеря з дівчини» замість «Вечеря з дівчиною») — бекенд відхиляв назву як вигадану, хоча користувач її вже сказав.

«Так» / «правильно» після запропонованої назви не підтверджували останній варіант: наступний `create` знову вимагав, щоб рядок назви буквально прозвучав у репліках.

### 1.2. Втрата дати й часу → `schedule_not_from_user`

Чернетка зберігала `date`/`time` лише коли `prepared.time` уже був у `_mentioned_times(blob)`.

«На 8 вечора» парсилось як `08:00`, а модель передавала `20:00`. Час у draft не потрапляв. Після уточнення лише назви («Вечеря вдома») перевірка знову вимагала дату й час з поточної репліки — попередній розклад губився.

### 1.3. `confirmation_required` без голосу

Результат tool ішов у Realtime як JSON, а follow-up `create_response(tool_choice="none")` міг deferитись, поки інша відповідь ще in flight. Модель іноді не перетворювала JSON на аудіо. Питання «Підтвердити?» не звучало, хоча `op_id` уже був у сесії.

Мутація Google без явного confirm як і раніше була заблокована pending-store; ламався саме голосовий крок підтвердження.

---

## 2. Модель стану розмови

Новий модуль `agents/create_draft.py` — `CreateDraftStore`:

| Поле | Призначення |
|---|---|
| `user_sub` | Google `sub` активного акаунта |
| `session_id` | голосова сесія (період awake) |
| `title` / `proposed_title` | підтверджена або запропонована назва |
| `date` / `time` / `duration_minutes` / `timezone` | слоти розкладу |
| `confirmed_fields` | які слоти вже з реплік користувача |
| `expires_at` | TTL незавершеного create (10 хв) |

Ключ: `(user_sub, session_id)`. Чужий акаунт / нова сесія / disconnect / `reauth_switch` / `lock_session` очищають draft.

Правила злиття:

1. Дата й час з реплік накопичуються, навіть поки немає назви.
2. Нове значення замінює старе лише якщо воно явно є в **останній** репліці.
3. Зміна назви не чистить розклад.
4. «Скасуй» / «не треба» скидає draft.
5. «Дякую» / «Алло» не стають назвою.
6. Після успішного ground → `confirmation_required` draft очищається; confirm yes/no також.

Зіставлення назви: точний підрядок, stemming українських закінчень, `SequenceMatcher` ≥ 0.82. Близька нормалізація (0.72–0.82) → `title_needs_confirm` («Правильно зрозуміла назву як …?»). «Так» приймає `proposed_title`.

Час: «N вечора/ранку/ночі» → 24-годинний формат; «на 8» також додає вечірній варіант `20:00`.

---

## 3. Гарантоване озвучення підтвердження

У `assistant._run_router_tool` для `confirmation_required`:

1. У розмову Realtime йде JSON (`status`, `message`, `op_id`) з `trigger_followup=False`.
2. Питання озвучується скриптом `rt.say(result.message)` — не залежить від того, чи модель прочитала JSON і чи `create_response` не застряг у черзі.
3. Якщо `say()` падає — fallback `create_response(tool_choice="none")`.

Жодна зміна в Google Calendar не виконується без `action=confirm` + валідного `op_id` (існуючий pending state machine без змін по суті).

---

## 4. Змінені файли

| Файл | Зміна |
|---|---|
| `agents/create_draft.py` | **новий** — draft store, fuzzy title, ack/cancel/yes |
| `agents/calendar_agent.py` | `_ground_create` на слотах; вечірній час; clear draft |
| `assistant.py` | `say()` для `confirmation_required`; `proposed_title` у JSON |
| `router/agent_router.py` | clear draft+pending на disconnect / switch / lock |
| `tests/test_calendar_conversation.py` | **новий** — наскрізні регресії |
| `CALENDAR_CONVERSATION_FIX_REPORT.md` | цей звіт |

Не змінювались: n8n (немає в runtime), `robot_control`, Gmail API-шлях поза очищенням draft, OAuth scopes.

---

## 5. Автотести

```text
uv run pytest -q
```

**Результат: 146 passed**, 0 failed, 0 skipped (3 warnings від `speech_recognition` / `webrtcvad`).

Нові сценарії в `tests/test_calendar_conversation.py`:

- дата/час → назва → confirm → рівно один insert;
- відмінок назви + «так» на `proposed_title`;
- зміна назви без втрати дати/часу;
- «Дякую» / «Алло» без операції;
- `confirmation_required` при in-flight відповіді → `say()`;
- «ні» без create;
- повторне «так» без дубліката;
- draft після disconnect недоступний іншому `sub`.

Регресії читання / edit / reschedule / delete (`test_calendar_agent`, `test_calendar_edit_delete`, `test_calendar_create_regression`, `test_calendar_confirm_voice`, `test_calendar_tool_args`) — зелені.

---

## 6. Ручна перевірка (Realtime + Google Calendar)

Окремий тестовий акаунт. Gmail send і робота не вмикати.

1. «Давай сьогодні додамо вечерю на 8 вечора» — питання про назву; у календарі порожньо.
2. «Вечеря з дівчиною» — голосом: «Створити подію … Підтвердити?» (обов’язково чутно).
3. «Так» — рівно одна подія з правильною назвою, сьогодні ~20:00.
4. Новий діалог: дата/час → «Дякую» → знову питання назви, без вигаданої події.
5. Дата/час → «Вечеря з дівчиною» → «Вечеря вдома» — підтвердження з **новою** назвою і **старими** датою/часом.
6. На підтвердженні «Ні» — події немає.
7. Повторне «Так» після успіху — другої події немає.
8. Disconnect / зміна акаунта — чужий користувач не бачить чужих слотів.

---

## 7. Залишкові ризики

1. Fuzzy title ≥ 0.82 може рідко прийняти близьку, але не ту фразу — тоді користувач все одно чує confirmation і може сказати «ні».
2. `say()` для confirm тримає питання поза `conversation` history (`conversation: none`); `op_id` лишається в function_call_output JSON.
3. TTL draft 10 хв; після паузи треба повторити параметри.
4. Живий e2e з мікрофоном у CI не ганявся — потрібен ручний прогін з розділу 6.
