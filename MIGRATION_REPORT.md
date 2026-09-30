# Звіт про міграцію: n8n → Google API

**Проєкт:** `voice-agent-v3-2` (канонічна копія voice-agent-v3-2-hryhorii)  
**Дата:** 2026-09-29  
**Режим:** локальний desktop MVP (OAuth loopback)

---

## 1. Архітектура до і після

### До

```
Мікрофон → Realtime API → dispatch_task
    → HTTP POST n8n webhook (/webhook/calendar-agent)
        → agent-ecosystem FastAPI (/api/v1/agent-router)
            → Google Calendar (OAuth токен усередині ecosystem)
```

Проблеми: зайвий хоп n8n (502 / «тихо не проксює»), жорсткий `N8N_ROUTER_USER_ID`, залежність від Docker n8n і окремого сервісу для базових Calendar/Gmail дій.

### Після

```
Мікрофон → Realtime API → google_account | calendar_action | gmail_action | dispatch_task
    → локальний AgentRouter (Python, in-process)
        → AccountManager + OS keyring (refresh tokens per Google `sub`)
        → Google Calendar API / Gmail API
```

n8n і зовнішній `agent-ecosystem` **не потрібні** для календаря та пошти. OpenAI Realtime, wake word, STT/TTS, barge-in, deferred tools, голос/мова, `robot_control` збережені.

Сусідній репозиторій `agent-ecosystem` **був доступний**: з нього перенесено ідеї confirmation gate, форматування часу, scopes Calendar, структуру envelope (`success` / `needs_more_info` / `confirmation_required` / `error`) + додано `auth_required` / `permission_denied`. Повний LLM-класифікатор, контакти, Zoom, recurrence planner **не** портувались цілком — замість цього typed Realtime tools.

---

## 2. Файли: створені / змінені / видалені

### Створені

| Шлях | Роль |
|---|---|
| `auth/__init__.py` | пакет OAuth |
| `auth/scopes.py` | мінімальні scopes |
| `auth/token_store.py` | keyring / in-memory store |
| `auth/google_oauth.py` | Desktop InstalledAppFlow |
| `auth/account_manager.py` | connect/status/switch/disconnect |
| `integrations/__init__.py` | пакет |
| `integrations/google_errors.py` | 401/403/429/5xx/мережа |
| `integrations/google_calendar.py` | Calendar API + FakeCalendarClient |
| `integrations/google_gmail.py` | Gmail API + sanitize + FakeGmailClient |
| `agents/__init__.py` | пакет |
| `agents/types.py` | `AgentResult` |
| `agents/pending_store.py` | pending ops per `sub` |
| `agents/calendar_agent.py` | календарний агент |
| `agents/gmail_agent.py` | поштовий агент |
| `router/__init__.py` | пакет |
| `router/agent_router.py` | локальний роутер |
| `router/factory.py` | wiring |
| `credentials/.gitkeep` | місце для client_secret.json |
| `data/.gitkeep` | стан акаунтів (без токенів) |
| `tests/helpers_google.py` | фейки для тестів |
| `tests/test_oauth_accounts.py` | OAuth / ізоляція акаунтів |
| `tests/test_calendar_agent.py` | календарні сценарії |
| `tests/test_gmail_agent.py` | Gmail + injection |
| `tests/test_google_migration.py` | без n8n, secrets, robot |
| `MIGRATION_REPORT.md` | цей звіт |

### Змінені

| Шлях | Що |
|---|---|
| `assistant.py` | tools + локальний router замість httpx→n8n |
| `config.py` | Google OAuth конфіг; прибрано N8N_* |
| `pyproject.toml` / `uv.lock` | google-*, keyring, tzdata |
| `.env.example` | нові змінні |
| `.gitignore` | secrets, credentials, data, .env |
| `README.md` | інструкція Google Cloud + запуск |
| `tests/conftest.py` | dummy env |
| `tests/test_check_connection.py` | Google-статуси |
| `tests/test_parse_router_reply.py` | AgentResult |

### Видалені

Функціонально видалено залежність від n8n/agent-ecosystem у runtime-коді. Окремі файли n8n у цьому каталозі не існували. Історичні згадки в `OVERVIEW.md` / `README-branch-note.md` лишені як архів (не використані кодом).

---

## 3. Основні зміни по файлах

- **`assistant.py`**: tools `google_account`, `calendar_action`, `gmail_action`; `dispatch_task` → `AgentRouter.handle_text`; `check_connection` без n8n; deferred threads збережені; watcher не тривожить на «ще не увійшли».
- **`config.py`**: `GOOGLE_OAUTH_CLIENT_SECRETS_FILE`, `GOOGLE_ACCOUNT_STATE_FILE`, `GOOGLE_CALENDAR_TIMEZONE`; оновлений SYSTEM_PROMPT.
- **`auth/*`**: Desktop OAuth, ідентичність за `sub`, токени в keyring.
- **`agents/*` + `integrations/*`**: бізнес-логіка Calendar/Gmail з підтвердженням людини.
- **`router/*`**: єдина точка входу без мережевого webhook.

---

## 4. Google OAuth і зберігання токенів

1. Desktop OAuth client JSON → `credentials/client_secret.json` (один Cloud Project розробника).
2. `InstalledAppFlow.run_local_server(port=0)` відкриває системний браузер (loopback MVP).
3. Після consent зберігається refresh token у **OS keyring** під ключем Google `sub`.
4. Access token оновлюється через `credentials.refresh()`; при RefreshError — `auth_required`.
5. Scopes: спочатку identity (+ calendar при connect); Gmail — окремо / `with_gmail=true`.
6. Відмова користувача → зрозуміле повідомлення, процес не падає.

**Не** використовується спільний plaintext `token.json` для всіх користувачів.

---

## 5. Ізоляція акаунтів

- Активний користувач = `active_sub` у `data/google_accounts.json` (без секретів).
- Pending operations у `PendingStore` ключуються **лише** `user_sub`.
- Switch дозволений лише між уже авторизованими акаунтами.
- Голос / названий email **не** є доказом особи.

Тест `test_two_accounts_no_pending_leak` підтверджує: підтвердження від Bob не виконує pending Alice.

---

## 6. Реалізовані функції Calendar / Gmail

### Calendar (реалізовано й покрито моками)

- Перегляд / пошук найближчих подій  
- Створення (з опційним Google Meet через `conferenceData`)  
- Скасування / перенесення  
- Обов’язкове підтвердження перед create/cancel/reschedule  
- Ідемпотентність повторного confirm після успіху  
- Часовий пояс `Europe/Kyiv` (+ `tzdata`)  
- Помилки 401/403/429/500 без фальшивого «готово»

### Gmail (реалізовано й покрито моками)

- Пошук, читання (тіло як недовірений текст)  
- Чернетка  
- Надсилання **лише** після confirm  
- Sanitize prompt-injection у тілі листа  

### Не портовано з agent-ecosystem (свідомо)

- Повний LLM intent classifier, контакти/групи, Zoom, складні recurrence, email-сповіщення учасникам як окремий transactional шар.

---

## 7. Результати тестів

**Команди:**

```bash
# uv sync виконано успішно (uv 0.12.20)
# Примітка: `uv run` на цій машині падав на canonicalize шляху з кирилицею;
# тести запускались так:
.\.venv\Scripts\python.exe -m pytest -q
```

**Результат:** **33 passed**, 0 failed, 0 skipped (з попередженнями speech_recognition/webrtcvad).

Покриті обов’язкові сценарії 1–11 ТЗ (моки Google).

Статичні перевірки окремого ruff/mypy у проєкті не було налаштовано — не запускались.

---

## 8. Що не перевірено без реального Google-акаунта

- Реальний браузерний OAuth consent і запис у Windows Credential Locker  
- Реальні виклики Calendar/Gmail API (квоти, Meet-лінк у проді, Gmail send)  
- Verified OAuth app для Gmail у production (поза Testing / test users)  
- Повна голосова сесія end-to-end з мікрофоном (потрібні OPENAI_API_KEY + аудіо)  
- Поведінка на macOS/Linux keyring backends  

---

## 9. Зміни поведінки / що могло зламатись

| Раніше | Тепер |
|---|---|
| Потрібні n8n + agent-ecosystem | Працює offline від них |
| Фіксований `N8N_ROUTER_USER_ID` | Акаунт = OAuth `sub` користувача |
| `check_connection` діагностував n8n hop | Діагностує Google auth/API |
| Пошта була «вимкнена» у промпті | Gmail знову доступний через tools |
| Вільний текст → n8n classification | Typed tools + спрощений local router |

Старі env `N8N_*` більше не читаються.

---

## 10. Ручна перевірка (від Google login до події)

1. Покласти Desktop client JSON у `credentials/client_secret.json`.  
2. Увімкнути Calendar API (+ Gmail API) у Cloud Console; додати себе в Test users.  
3. `cp .env.example .env` → `OPENAI_API_KEY=...`  
4. `uv sync` (або оновити `.venv`)  
5. `.\.venv\Scripts\python.exe main.py` (або `uv run python main.py`, якщо trampoline працює)  
6. «привіт» → «підключи Google» → увійти в браузері.  
7. «створи зустріч Тест на YYYY-MM-DD о 15:00» → «Підтвердити?» → «так».  
8. Перевірити подію в https://calendar.google.com  

---

## 11. Обмеження, ризики, наступні кроки

**Обмеження**

- OAuth loopback — desktop MVP, не публічний web.  
- Gmail sensitive scopes → Google verification для широкого rollout.  
- Якщо keyring недоступний — fallback in-memory (токени зникнуть після рестарту; у логах warning).  
- У робочій директорії був файл `client_secret_*.json` — він у `.gitignore`; **перенесіть** його в `credentials/client_secret.json` і не комітьте. Якщо секрет світився в чаті/диску — ротуйте Client Secret у Cloud Console.

**Git**

- Репозиторій на гілці `all-agents` був **брудний** (видалення `voice-agent-v3-2-hryhorii/` + untracked `voice-agent-v3-2/`). Гілку `feature/google-api-migration` **не створювали**, щоб не затерти незакомічену роботу. Зміни зараз у каталозі `voice-agent-v3-2/`.

**Наступні задачі**

1. Ручний OAuth + створення реальної події.  
2. Закомітити `voice-agent-v3-2` окремим PR (без client_secret).  
3. За бажанням — поглибити класифікацію (LLM) або підключити контакти з ecosystem.  
4. Production: web OAuth / verified app, якщо вихід за межі desktop.

---

## Базовий стан до змін (Етап 0)

- Git: `all-agents`, dirty (deleted hryhorii tree, untracked v3-2).  
- Baseline pytest (старий код): 1 failed (`test_both_broken_reports_server_down` — encoding/assert на n8n message) + KeyboardInterrupt у середині прогону; частина тестів встигла пройти.  
- `agent-ecosystem` і `n8n-agents-hryhorii` сусіди — доступні для аудиту.
