# voice-agent-v3-2 — голосовий асистент з прямими Google API (без n8n)

**Статус:** міграція з n8n → Google Calendar + Gmail OAuth (локальний desktop MVP)

## Що робить

Голосовий асистент: wake word → **voice engine** (`VOICE_ENGINE=live` — основний, або `realtime` — запасний) →
локальний **Agent Router** → **Google Calendar / Gmail / нотатки (Drive+Docs)** через OAuth Desktop flow.
n8n і зовнішній `agent-ecosystem` **не потрібні**.

- **live** (за замовчуванням): GPT-Live (`gpt-live-1`) + Responses delegation — Calendar + Gmail + Notes
  (`notes_add`, `notes_read`, `notes_search`, …). Перебивається лише на справжній намір
  (стоп-слово або людина перехоплює слово), а не на «угу», фоновий шум чи ехо.
- **realtime**: legacy OpenAI Realtime — запасний варіант.

Користувач входить своїм Google-акаунтом у системному браузері. Один Google Cloud Project
належить розробнику застосунку; кінцевий користувач не створює workflow і не вводить API-ключі.

## Tools

| Tool | Призначення |
|---|---|
| `google_account` | connect / status / grant_gmail / grant_notes / switch / disconnect |
| `calendar_action` | list / search / create / reschedule / cancel / confirm |
| `gmail_action` | search / read / draft / send / confirm |
| `notes_action` | add / read / search (Google Doc «Нотатки від агента») |
| `web_search` | контрольований пошук в інтернеті (Tavily; новини / факти / версії ПЗ) |
| `dispatch_task` | вільний текст → той самий локальний роутер (сумісність) |
| `check_connection` | стан Google-акаунта / API (не «чи живий n8n») |
| `control_robot` | фізичні команди (`robot_control.py`, StubBackend за замовчуванням) |
| `set_assistant_name`, `change_voice`, `change_language`, `end_conversation`, `note_emotion` | як раніше |

## Встановлення

### 1. Python / залежності

Потрібні **Python 3.12** і [uv](https://github.com/astral-sh/uv).

```bash
cd voice-agent-v3-2
uv sync
cp .env.example .env   # вписати OPENAI_API_KEY
```

### 2. Google Cloud (один раз, розробник застосунку)

1. Створіть проєкт у [Google Cloud Console](https://console.cloud.google.com/).
2. Увімкніть **Google Calendar API**, **Gmail API**, **Google Drive API** і **Google Docs API**.
3. **OAuth consent screen** → External (або Internal для Workspace).
   - Для тестового режиму додайте email тестових користувачів у Test users.
   - Gmail scopes часто вимагають верифікації Google для production — у Testing
     режимі працюють лише test users (обмеження Google, не обходимо).
4. **Credentials** → Create Credentials → **OAuth client ID** → тип **Desktop app**.
5. Завантажте JSON і збережіть як:

```text
credentials/client_secret.json
```

Файл уже в `.gitignore`. Не комітьте його.

### 3. Запуск асистента

```bash
# GPT-Live (default) — Calendar + Gmail + Notes
uv run python main.py

# Legacy Realtime (fallback) — set VOICE_ENGINE=realtime in .env, or
# Windows PowerShell:
$env:VOICE_ENGINE="realtime"; uv run python main.py
```

Скажіть «привіт», потім «підключи Google» — відкриється браузер, оберіть акаунт і
надайте дозволи (спершу Calendar; Gmail / нотатки — incremental при першому використанні
або через `grant_gmail` / `grant_notes`).

Нотатки зберігаються в Google Doc **«Нотатки від агента»** (маркер `appProperties`, не лише назва).
Деталі: `GOOGLE_DOCS_NOTES_IMPLEMENTATION_REPORT.md`.

Refresh tokens зберігаються в **OS keyring** (не в спільному `token.json`).

### Змінні `.env`

| Змінна | Навіщо |
|---|---|
| `OPENAI_API_KEY` | обов'язково |
| `VOICE_ENGINE` | `live` (default) або `realtime` (запасний legacy) |
| `OPENAI_LIVE_MODEL` | default `gpt-live-1` |
| `OPENAI_LIVE_BACKEND_MODEL` | Responses backend (default `gpt-6-luna`) |
| `OPENAI_LIVE_VOICE` | Live TTS voice (default `marin`) |
| `OPENAI_LIVE_AUDIO_RATE` | default `24000` |
| `VOICE_LOCAL_BARGE_IN` | локальний VAD barge-in (default `true`) |
| `VOICE_BARGE_IN_CONFIRM_MS` | скільки чекати доказів наміру перебити після duck (default `1200`) |
| `VOICE_BARGE_IN_MIN_SPEECH_MS` | перебивання лише за звуком, без транскрипції: стільки безперервної мови (default `600`) |
| `VOICE_BARGE_IN_REJECT_SILENCE_MS` | тиша, що закриває короткий звук; покриває паузи між словами (default `300`) |
| `VOICE_BUSY_CUES_ENABLED` | короткі «Угу.» під час довгих tools (default `true`) |
| `REALTIME_SILENCE_MS` | пауза, що завершує репліку в realtime (default `600`); менше = швидша відповідь, але може обрізати |
| `GOOGLE_HTTP_TIMEOUT_S` | таймаут одного запиту до Google API (default `15`) |
| `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` | шлях до Desktop client JSON |
| `GOOGLE_ACCOUNT_STATE_FILE` | активний `sub` + display profiles (без секретів) |
| `GOOGLE_CALENDAR_TIMEZONE` | дефолт `Europe/Kyiv` |
| `CONNECTIVITY_CHECK_INTERVAL_S` | фоновий health (сек) |
| `SHARED_DEVICE_MODE` | `true` = спільний ПК/робот (idle lock); `false` = особистий desktop |
| `SESSION_IDLE_TIMEOUT_S` | таймаут бездіяльності сесії Google (сек) у shared mode |
| `ROBOT_BACKEND` | `stub` / `go2` / `humanoid` |

### Режими пристрою

**Особистий desktop (`SHARED_DEVICE_MODE=false`, за замовчуванням):** активний Google-акаунт
зберігається між запусками; зручно для одного користувача на своєму ПК.

**Спільний ПК / робот (`SHARED_DEVICE_MODE=true`):** після idle timeout або «заблокуй сесію»
`active_sub` скидається — наступна людина не отримає автоматичний доступ до чужої пошти.
Після restart сесія також не відновлюється автоматично. Refresh tokens у keyring лишаються,
але активна сесія — ні.

### Тести і перевірки коду (без реальних Google credentials)

```bash
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts
uff.exe check .      # lint (також як pre-commit hook)
.\.venv\Scripts\mypy.exe .            # типи (поки інформативно, не блокує)
```

> `uv run` не працює, якщо шлях до проєкту містить кирилицю/пробіли (`Мій ПК`,
> `Робочий стіл`) — тоді викликайте `.venv\Scripts\...` напряму або перенесіть
> проєкт, напр. у `C:\devoice-agent` (заодно поза OneDrive).

### Структура коду

| Де | Що |
|---|---|
| `assistant.py` | життєвий цикл (сон ↔ сесія), пам'ять, спільні tool-хелпери |
| `voice/realtime_driver.py`, `voice/live_driver.py` | сесія на кожному рушії + його tool-обробники |
| `router/agent_router.py` | локальний роутер; вільний текст → таблиця інтентів |
| `agents/calendar_agent.py` + `calendar_create/edit/execution.py` | агент календаря (міксини по флоу) |
| `agents/calendar_speech/events/validation.py` | чисті хелпери: мовлення, події, валідація |
| `integrations/google_http.py` | спільний транспорт Google: пул з'єднань, таймаут, повтори читань |
| `tools/realtime_schemas.py`, `tools/live_schemas.py` | схеми tools для кожного рушія |

### Ручна перевірка календаря

1. `uv run python main.py` (або `.venv\Scripts\python.exe main.py`)
2. «привіт» → «підключи Google» → увійти в браузері.
3. «постав зустріч Демо на <дата> о 15:00» → почути «Підтвердити?».
4. «так» → дочекатися підтвердження від Google API (не раніше).
5. Перевірити подію в Google Calendar у веб-інтерфейсі.

## Безпека (MVP)

- Loopback OAuth (`run_local_server`) — **лише для локального desktop**, не публічний web.
- Голос ≠ ідентичність: доступ вимагає явного connect/switch через браузер.
- Фактичні `granted_scopes` зберігаються окремо (envelope v2), не лише `Credentials.to_json()`.
- Листи — недовірений вміст; інструкції з тіла листа не виконуються.
- Мутації Calendar/Gmail — лише після «так»; concurrent confirm ідемпотентний.
- Чернетка перед send перечитується з Google; зміна вмісту блокує send.

Детальний звіт міграції: [`MIGRATION_REPORT.md`](./MIGRATION_REPORT.md).
Останні виправлення аудиту: [`NEXT_FIX_REPORT.md`](./NEXT_FIX_REPORT.md).
Технічний огляд (частково історичний): [`OVERVIEW.md`](./OVERVIEW.md).
