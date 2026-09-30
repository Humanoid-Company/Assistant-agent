# Голосовий асистент k11 — огляд проєкту

Україномовний голосовий асистент на базі OpenAI Realtime API (`gpt-realtime`). Слухає слово-пробудження дешевим Google STT, а після пробудження веде всю розмову через один персистентний Realtime-сеанс — асистент чує користувача напряму, сам вирішує коли говорити, і викликає інструменти (tools) для команд.

---

## Архітектура

```
Мікрофон (один persistent sounddevice stream)
   │
   ▼
SLEEPING ── SpeechToText (Google STT) — чекає слово-пробудження
   │           "привіт" / "агент" / "асистент" / "гей агент"
   ▼
AWAKE ──── RealtimeConversation (OpenAI Realtime API, WebSocket)
   │           стрімить мікрофон у сесію, грає аудіо-відповідь,
   │           сервер сам вирішує коли мовлення користувача закінчилось (VAD)
   │
   ├──► TOOLS (function calling, не regex):
   │       set_assistant_name    — змінити ім'я асистента (диск, довготривало)
   │       change_voice          — змінити TTS-голос (диск; НАБУВАЄ ЧИННОСТІ ЛИШЕ ПІСЛЯ ПЕРЕЗАПУСКУ)
   │       change_language       — змінити мову спілкування (диск; застосовується ОДРАЗУ, без перезапуску)
   │       end_conversation      — заснути (мовчки, без власної фрази)
   │       note_emotion          — службовий: емоція голосу (паралельно з відповіддю)
   │       dispatch_task          — ЄДИНА точка входу в n8n (Router), що сама
   │                                 класифікує й форвардить: календар, пошта,
   │                                 research/notes/translate/… (фоново)
   │       control_robot          — фізична команда роботу (фоново; поки що StubBackend)
   │
   └──► TextToSpeech (OpenAI TTS) — лише одна стартова фраза перед пробудженням
```

### Стани асистента (`assistant.py`)

| Стан | Що відбувається |
|---|---|
| `SLEEPING` | `SpeechToText.listen()` чекає одну з `TRIGGER_PHRASES` |
| `AWAKE` | Один `RealtimeConversation` на всю розмову, до виклику `end_conversation` |

Немає окремих станів LISTENING/SPEAKING/PROCESSING — сервер Realtime API сам керує чергою мовлення (`server_vad`, `create_response=False`): клієнт вирішує коли створити відповідь (`create_response()`), сервер — коли користувач договорив і коли перебив асистента (`interrupt_response=True`).

---

## Пам'ять

**В межах одного запуску процесу** (`Assistant._history`): репліки розмови переживають цикли сну/пробудження (сказали "до побачення", потім знову "привіт" — асистент пам'ятає). При виклику `end_conversation` завершальний обмін (прощання) обрізається з історії перед збереженням (`_history_cutoff`) — інакше наступна сесія бачила готове прощання в контексті і одразу ж намагалась завершитись сама (був такий баг, виправлено).

**Довготривало на диску** (`assistant_memory.json`): ім'я асистента (`set_assistant_name`), обраний голос (`realtime_voice`, `change_voice`) і мова спілкування (`language`, `change_language`) — усі переживають перезапуск скрипта.

**Echo-baseline між сесіями** (`Assistant._echo_baseline`, окремо від `assistant_memory.json` — тримається лише в пам'яті процесу): оцінка рівня власного відлуння асистента з `realtime_client.py`'s echo-гейту тепер передається з однієї `RealtimeConversation` в наступну (при кожному новому пробудженні), а не скидається до `0.0` щоразу. Раніше кожне нове "привіт" починало підстройку з нуля й покладалось лише на статичний floor (`_ECHO_RMS_FLOOR`), тож перші миті після зміни гучності колонок могли хибно розпізнаватись як перебивання користувачем. Обнуляється лише при повному перезапуску процесу.

Розпізнавання голосу/спікерів **відсутнє** (було видалено разом із персональними фактами/тоном per-людина — свідоме рішення спростити проєкт).

---

## Модулі

### `config.py`
Усі налаштування, `.env` через `python-dotenv`. `OPENAI_API_KEY` обов'язковий (кидає `EnvironmentError` якщо відсутній). Ключові розділи:
- `REALTIME_MODEL` / `REALTIME_VOICE` / `REALTIME_SILENCE_MS` — Realtime API
- `SYSTEM_PROMPT` — інструкції моделі (стиль, емоції, коли викликати які tools); **не** містить мову спілкування хардкодом — `assistant.py._build_instructions()` дописує актуальну мову (`LANGUAGE_OPTIONS`, дефолт "українською") і, якщо задане, ім'я асистента поверх базового промпту
- `TRIGGER_PHRASES` — слова пробудження
- `N8N_*` — URL і Header-Auth токени для двох вебхуків (calendar/email), порожні за замовчуванням — відповідний tool каже "не налаштовано", а не падає
- `STT_*` — тюнінг Google STT для SLEEPING-режиму

### `speech_to_text.py`
`SpeechToText` — мікрофон через один persistent `sounddevice.RawInputStream` (відкривається раз при старті). Використовується у двох ролях:
- `listen()` — запис фрази з WebRTC VAD (onset/silence) + Google STT транскрипція. Тільки для SLEEPING.
- `read_chunk()` — сирі PCM-чанки для `RealtimeConversation`'s mic-feeder потоку, коли AWAKE. Той самий стрім, що й для sleep-detection — важливо не відкривати другий, інакше аудіо ділиться між двома читачами.

### `text_to_speech.py`
`TextToSpeech` — OpenAI TTS (`tts-1`, голос `nova`) через `pygame`. Використовується ЛИШЕ для однієї стартової фрази при запуску, до першого пробудження — уся жива розмова озвучується через `RealtimePlayer` в `realtime_client.py`.

### `realtime_client.py`
Обгортка над OpenAI Realtime API (одна persistent WebSocket-сесія).

**`RealtimePlayer`** — грає PCM16@24kHz аудіо-дельти в окремому потоці (`queue.Queue`). `is_active` враховує грейс-період — використовується щоб приглушити мікрофон під час мовлення асистента (щоб не чув сам себе).

**Ехо-гейт** (`_ECHO_*` константи) — без апаратного AEC мікрофон ловить власний голос асистента з динаміків; чанки тихіші за ковзний baseline echo-рівня відкидаються, гучніші (реальне перебивання) пропускаються. `_echo_baseline` тепер приймається як параметр конструктора (`echo_baseline=`, дефолт `0.0`) і читається назад через `.echo_baseline` property після `close()` — `assistant.py` носить це значення між пробудженнями (див. "Пам'ять" вище).

**`RealtimeConversation`** — головний клас:
- Конструктор приймає `voice` (дефолт `config.REALTIME_VOICE`) і `echo_baseline` (дефолт `0.0`) — обидва передаються з `assistant.py` на основі збереженого стану.
- `connect()` — піднімає WebSocket (з `self._voice`, не глобальною константою), стартує reader/feeder потоки і плеєр
- `pump()` — обробляє події з черги; повертає PCM завершеної репліки користувача
- `say()` — скриптована фраза (`conversation: none`, `tool_choice: none` — не може сама викликати tool, це запобігало випадковому end_conversation під час привітання)
- `create_response()` — звичайна відповідь моделі
- `update_instructions(text)` — живий `session.update` з новим текстом інструкцій (ім'я асистента, мова спілкування) — застосовується одразу, без перепідключення.
- `update_transcription_language(code)` — так само живий `session.update`, але для мови вхідної транскрипції (`audio.input.transcription.language`). На відміну від голосу (`voice` — фіксується на весь конекшн, Realtime API не дозволяє змінити його без перепідключення), мова застосовується миттєво.
- Tool calls: `silent_tools` (напр. `note_emotion`) не тригерять follow-up; `no_followup_tools` (напр. `end_conversation`, `change_voice`) — результат повертається без автоматичної follow-up відповіді, бо викликаючий код сам озвучує результат/модель сама коротко прощається перед заплановим завершенням сесії.
- `submit_deferred_tool_result()` — для повільних (async, HTTP) tool-викликів: обробник одразу повертає `None`, фоновий потік потім сам здає результат назад у сесію
- `inject_history()` / `get_turns()` — відновлення/збереження реплік між awake-сесіями (для `Assistant._history`)

### `assistant.py`
Машина станів, координує все.

**TOOLS**: `set_assistant_name`, `change_voice`, `change_language`, `end_conversation`, `note_emotion`, `dispatch_task`, `control_robot`.

**`_handle_robot_action()`** — `control_robot` виконується у фоновому потоці (той самий патерн, що й `_dispatch_task`), результат повертається через `submit_deferred_tool_result`. Реальне виконання йде через `self.robot` (`robot_control.py`) — див. розділ нижче.

**`_run_awake_session()`**: конектиться (з збереженими `voice`/`echo_baseline`), відновлює історію (якщо є), каже "Слухаю!", у циклі `pump()` → `create_response()` на кожну репліку користувача. При `end_conversation`: чекає завершення поточної відповіді → `player.stop()` (прибрати залишки в черзі) → каже "До побачення!". При `change_voice` (прапорець `_voice_change_pending`) — той самий шлях завершення сесії, але **без** скриптованого "До побачення!" (модель уже сама попрощалась у своїй відповіді на tool-виклик) і після закриття сесії `self._running = False` замість повернення в SLEEPING — процес завершується повністю, новий голос застосовується лише при наступному `python main.py`.

**`_change_voice(voice)`** — валідує проти `VOICE_OPTIONS` (10 фіксованих пресетів Realtime API, кастомні голоси неможливі), зберігає в пам'ять, ставить `_voice_change_pending=True` і `_sleep_requested=True`.

**`_change_language(language)`** — валідує проти `LANGUAGE_OPTIONS` (`uk`/`ru`/`en`), зберігає в пам'ять, і якщо сесія жива — одразу шле `rt.update_instructions()` + `rt.update_transcription_language()`. На відміну від голосу, перезапуск не потрібен і сесія не завершується.

**`_dispatch_task()`**: `dispatch_task` — HTTP POST `{"task": "..."}` у n8n Router на фоновому потоці (не блокує розмову); власна відповідь роутера (`{"text": "..."}`) повертається в сесію через `submit_deferred_tool_result` і озвучується як є.

**`_sanitize_name()`** / `_NOT_A_NAME` — захист від сміттєвих імен (займенники, окремі літери) при зміні імені асистента.

---

## Фізичне керування роботом (`robot_control.py`)

Заліза (Unitree Go2 / G1 / R1 EDU) ще немає — модуль лише готує "гачки" наперед:

- `RobotController`-словник дій (`ROBOT_ACTIONS`): `move_forward`, `move_backward`, `turn_left`, `turn_right`, `stop`, `sit`, `stand_up`, `stand_down`, `greet` — спільний набір і для собаки (Unitree SDK2 `SportClient`), і для гуманоїда (`LocoClient` + `G1ArmActionClient`).
- `StubBackend` — єдина реалізація, що працює зараз: тільки логує намір, нічого фізично не робить.
- `Go2Backend` / `HumanoidBackend` — заготовки, кидають `NotImplementedError`; дописуються реальними викликами `unitree_sdk2_python`, коли з'явиться відповідний робот.
- `ROBOT_BACKEND` (config.py, з `.env`: `stub` | `go2` | `humanoid`) і `ROBOT_NETWORK_INTERFACE` — перемикають, який backend створює `create_robot_controller()`. `assistant.py` і tool-шар (`control_robot`) від конкретного робота не залежать — міняти доведеться лише `robot_control.py`.

Робот, ким би він не був (Go2 точно, гуманоїд — G1 або R1 EDU під питанням), під'єднується через **CycloneDDS**, а не патчем прошивки: наш процес — окрема програма-клієнт на тій самій шині, що й заводський control-stack, запущена або прямо на Jetson робота, або на окремому комп'ютері в тій самій мережі.

### Детерміновані тригери команд (обхід моделі)

Рішення моделі викликати `control_robot` через tool-calling виявилось ненадійним на практиці — замість виконання команди модель іноді просто жартувала/розмовляла у відповідь. Тому фізичні команди тепер ловляться **до** того, як хід взагалі доходить до моделі:

- `ROBOT_TRIGGER_PHRASES` (config.py) — словник фраза → дія (`"іди вперед"` → `move_forward` тощо).
- У циклі `_run_awake_session` (assistant.py) кожна щойно завершена репліка користувача спершу проганяється через `self._check_robot_trigger()` → `RealtimeConversation.pump_for_transcript()` — чекає (до 1.5с) транскрипцію ЦІЄЇ Ж сесії (`gpt-4o-mini-transcribe`, `conversation.item.input_audio_transcription.completed`), звірену з `ROBOT_TRIGGER_PHRASES` через word-boundary regex (`_match_robot_trigger`). **Спершу пробували окремий Google STT-прохід на тому самому PCM — два різні STT-рушії розходились у розпізнаванні того самого звуку (сесія чула "Іде вперед", паралельний Google STT — щось інше), тож тригер мовчки не спрацьовував.** Використання транскрипції самої сесії прибирає цю розбіжність.
- Якщо є збіг — `_execute_robot_trigger()` виконує дію на `self.robot` напряму і озвучує підтвердження через `rt.say()` (скриптовано, без участі моделі) — `rt.create_response()` для цього ходу взагалі не викликається, тож модель фізичну команду ніяк не "обдумує" і пожартувати не може.
- Якщо збігу нема — усе працює як раніше (`rt.create_response()`, модель сама вирішує, чи викликати `control_robot` як tool — залишається запасним шляхом для формулювань поза списком тригерів).

Компроміс: очікування транскрипції додає до ~1.5с (типово значно менше — вона вже жваво стрімиться) до **кожного** ходу в AWAKE, не лише фізичних команд.

---

## Диспетчеризація через n8n Router (`dispatch_task`)

**v3(2) відрізняється від v3 саме тут.** У v3 Python-код тримав `AGENT_REGISTRY` з 12 окремих n8n-агентів плюс два прямі tools для календаря/пошти (14 webhook-адрес усього) — модель сама обирала, який з них викликати. У v3(2) увесь цей вибір передано в n8n: асистент знає лише про ОДИН webhook — `Router: Dispatch to Agent` (`/webhook/router`) — і завжди шле туди вільний текст завдання; Router (n8n workflow, вже існував у v3, але раніше використовувався тільки іншими клієнтами) сам класифікує запит і форвардить у потрібний нижчий workflow (календар, пошта, research, notes, translate, writer, summarize, news, currency, image, scheduler, weather, directions тощо).

- `N8N_ROUTER_WEBHOOK_URL` / `N8N_ROUTER_WEBHOOK_TOKEN` (config.py, `.env`) — єдина пара змінних для всієї делегації. Немає окремого реєстру агентів у Python.
- `ROUTER_TASK_CATEGORIES` (config.py) — рядок-підказка моделі, які категорії запитів варто слати в `dispatch_task`; сама класифікація й маршрутизація відбувається всередині Router, не в Python.
- Tool `dispatch_task(task)` — завжди присутній у `TOOLS` (на відміну від v3, де `delegate_to_agent` з'являвся, лише якщо реєстр був непорожній).
- `_dispatch_task()` — той самий фоновий-потік патерн, що був у `_handle_agent_delegation`/`_call_n8n_webhook`: **реальна відповідь Router'а (`{"text": "..."}`) і Є тим, що асистент скаже вголос**, не канонічний "успіх/невдача".
- **Щоб додати нового агента: нічого міняти в цьому коді не треба** — досить створити/розширити workflow в n8n і навчити Router його розпізнавати. `assistant.py`/`config.py` v3(2) лишаються незмінними.

Технічна пастка n8n, актуальна й тут, якщо колись знову генерувати великий JSON усередині `{{ }}`-виразу: шаблонізатор шукає **перше** входження `}}` як кінець виразу, а не рахує баланс дужок — вкладений JSON (`"required": [...]}}`) обриває вираз занадто рано ("invalid syntax"). Рішення: тримати статичний JSON літеральним текстом поза `{{ }}`, а в сам вираз загортати лише малу динамічну частину (`{{ JSON.stringify($json.body.task) }}`).

Також важливо (успадковано з v3): початкова інструкція Router'а НЕ повинна вимагати обов'язкового виклику якогось tool, інакше нейтральні фрази ("привіт, як справи?") хибно направляються у випадковий агент. Router має вміти відповісти звичайним текстом, якщо запит не є завданням для жодного агента.

---

## n8n інтеграція

Один вебхук у локальному n8n (`localhost:5678`, Docker), яким користується цей Python-код: `router` → класифікує й форвардить у Google Calendar / Gmail / інші workflows. Захищений Header Auth (токен у `.env`). Модель викликає `dispatch_task` лише коли користувач явно попросив — не вигадує дані, яких не називали. Google Calendar/Gmail credentials авторизуються всередині відповідних n8n-workflows через OAuth2 (Client ID/Secret з окремого Google Cloud проєкту) — це не змінюється відносно v3.

---

## Залежності

```
openai[realtime], SpeechRecognition, sounddevice, pygame-ce, webrtcvad,
numpy<2, python-dotenv, httpx, setuptools<81 (webrtcvad+pkg_resources)
```

Python 3.12 (venv). `pygame-ce`/`sounddevice` замість `pyaudio`/`pygame` — кращі wheels на нових версіях Python.

`.env` (не в git): `OPENAI_API_KEY`, `N8N_ROUTER_WEBHOOK_URL`, `N8N_ROUTER_WEBHOOK_TOKEN`, `GOOGLE_OAUTH_CLIENT_ID/SECRET` (усередині n8n, не тут).

---

## Структура файлів

```
v3(2)/
├── main.py              — точка входу, налаштування логування
├── assistant.py         — машина станів SLEEPING/AWAKE, tools, dispatch_task
├── realtime_client.py   — OpenAI Realtime API клієнт (WebSocket, плеєр, ехо-гейт)
├── speech_to_text.py    — Google STT (wake-phrase) + mic-чанки для Realtime
├── text_to_speech.py    — OpenAI TTS для стартової фрази
├── config.py            — усі налаштування, SYSTEM_PROMPT, N8N_ROUTER_*
├── assistant_memory.json — ім'я асистента (єдина довготривала пам'ять)
├── .env                 — секрети (не в git)
├── .env.example         — шаблон
└── requirements.txt     — залежності
```
