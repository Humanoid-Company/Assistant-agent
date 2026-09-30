# voice-agent-v3-2 — голосовий асистент з прямими Google API (без n8n)

**Статус:** міграція з n8n → Google Calendar + Gmail OAuth (локальний desktop MVP)

## Що робить

Голосовий асистент: wake word → OpenAI Realtime API → локальний **Agent Router** →
**Google Calendar / Gmail** через OAuth Desktop flow. n8n і зовнішній `agent-ecosystem`
**не потрібні** для календаря та пошти.

Користувач входить своїм Google-акаунтом у системному браузері. Один Google Cloud Project
належить розробнику застосунку; кінцевий користувач не створює workflow і не вводить API-ключі.

## Tools

| Tool | Призначення |
|---|---|
| `google_account` | connect / status / switch / disconnect (браузерний OAuth) |
| `calendar_action` | list / search / create / reschedule / cancel / confirm |
| `gmail_action` | search / read / draft / send / confirm |
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
2. Увімкніть **Google Calendar API** і **Gmail API**.
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
uv run python main.py
```

Скажіть «привіт», потім «підключи Google» — відкриється браузер, оберіть акаунт і
надайте дозволи (спершу Calendar; Gmail — при першому використанні пошти або
`google_account` з `with_gmail=true`).

Refresh tokens зберігаються в **OS keyring** (не в спільному `token.json`).

### Змінні `.env`

| Змінна | Навіщо |
|---|---|
| `OPENAI_API_KEY` | обов'язково |
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

### Тести (без реальних Google credentials)

```bash
.\.venv\Scripts\python.exe -m pytest -q
```

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
