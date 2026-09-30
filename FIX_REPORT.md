# FIX_REPORT — безпека Google OAuth / Calendar / Gmail (desktop MVP)

**Проєкт:** `voice-agent-v3-2`  
**Дата:** 2026-09-29  
**Архітектура збережена:** Realtime → AgentRouter → Google OAuth → Calendar/Gmail agents → Google APIs (без n8n)

---

## A. Root causes

1. **OAuth scopes.** `Credentials.from_authorized_user_info(raw, list(ALL_KNOWN_SCOPES))` підставляв повний список Calendar+Gmail при завантаженні токена, навіть якщо користувач надав лише Calendar. `ensure_scopes` також мовчки відкривав incremental flow замість явного `permission_required`.

2. **Reschedule.** `propose_reschedule` спочатку викликав `propose_cancel` (створював pending `calendar_cancel`), і лише потім парсив нову дату. Значення на кшталт `2099-02-30` проходили regex `\d{4}-\d{2}-\d{2}`, pending cancel уже існував; подальше `так` могло **видалити** подію замість перенесення. Час `25:90` також проходив `\d{2}:\d{2}`.

3. **Account isolation.** `AccountManager.switch(email)` дозволяв голосове перемикання на раніше авторизований акаунт за названим email — на спільному роботі це cross-account access без повторного OAuth.

4. **Pending / errors.** Не вистачало явного `permission_required` / `rate_limited` / `not_found`; TTL був, але reschedule-помилки могли лишати небезпечний pending.

---

## B. Changes (файли)

| Файл | Що зроблено |
|---|---|
| `auth/scopes.py` | Окремі Gmail readonly/compose/send; `scope_labels()` |
| `auth/google_oauth.py` | Load **без** ALL_KNOWN_SCOPES; `require_scopes` / `request_scopes`; `permission_required` |
| `auth/account_manager.py` | Гранулярний status; `request_gmail_permission`; `switch_via_reauth`; прибрано email-`switch`; disconnect лише активного |
| `agents/types.py` | Статуси `permission_required`, `not_found`, `rate_limited`; `result_from_google_error` |
| `agents/pending_store.py` | TTL 5 хв; ownership check; cleanup helpers |
| `agents/calendar_agent.py` | Атомарний reschedule; `validate_date_time` до pending; ігнор `user_sub` від LLM |
| `agents/gmail_agent.py` | Ті самі error/ownership правила; ігнор `user_sub` |
| `integrations/google_calendar.py` | `validate_date_time` (календарна валідність + 00–23:00–59) |
| `integrations/google_gmail.py` | Wrap untrusted content **без** вирізання тексту |
| `integrations/google_errors.py` | 404 / 409 mapping |
| `router/agent_router.py` | `grant_gmail`, `reauth_switch`; фрази для Gmail consent |
| `assistant.py` | Tool `google_account` без email-switch; `permission_required` у parse |
| `tests/helpers_google.py` | FakeOAuth з real scope semantics |
| `tests/test_oauth_scopes.py` | Нові regression |
| `tests/test_reschedule_safety.py` | Нові regression |
| `tests/test_account_isolation.py` | Нові regression |
| `tests/test_oauth_accounts.py` | Оновлено під нову модель |
| `tests/test_calendar_agent.py` | 429 → `rate_limited` |
| `tests/test_gmail_agent.py` | Injection wrap expectations |
| `MANUAL_GOOGLE_TESTS.md` | Чекліст для PM/QA |
| `FIX_REPORT.md` | Цей звіт |

Не змінювались: Realtime/STT/TTS/barge-in/`robot_control`/n8n (n8n і так відсутній у runtime).

---

## C. OAuth scope handling

- У keyring зберігається JSON credentials **як повернув Google** (`to_json()`), включно з полем `scopes`.
- При load: `Credentials.from_authorized_user_info(info)` **без** другого аргументу scopes.
- Перед API: `require_scopes(sub, required)` — якщо бракує scope → `OAuthError(permission_required)` → `AgentResult(permission_required)`; **Gmail API не викликається**.
- Incremental consent: лише явна дія `grant_gmail` / `request_gmail_permission` / connect з `with_gmail=true`.
- Status розрізняє: `calendar_ready`, `gmail_readonly_ready`, `gmail_compose_ready`, `gmail_send_ready`, `gmail_ready`.

---

## D. Account isolation

- Canonical id = Google **`sub`** активного session state (`data/google_accounts.json` + keyring).
- LLM/tool args `user_sub` / `email` **ігноруються** агентами.
- Голосовий switch за email **видалено**. Зміна акаунта = `reauth_switch` → новий browser OAuth (account chooser).
- `disconnect(foreign_sub)` → `forbidden_switch`.
- Pending ключується `user_sub`; confirm Bob не бачить / не виконує pending Alice.

---

## E. Reschedule fix

**Було:** cancel-pending → потім validate date → при `2099-02-30` pending cancel лишався → «так» видаляло подію.

**Стало:**

1. `validate_date_time` (формат + календарна дата + година/хвилина)
2. Resolve event **без** pending
3. Лише тоді `pending.put(kind=calendar_reschedule, …)` з `event_id`, original, new start/end, timezone, owner sub
4. Confirm виконує один `events.patch`

Немає двох незалежних pending cancel+create.

---

## F. Pending operations

Поля: `op_id`, `user_sub`, `kind`, `summary_uk`, `payload`, `created_at`, `expires_at`, `executed`, `execution_result`.

- TTL за замовчуванням **300 с** (5 хв); після success — коротке вікно для idempotent duplicate confirm.
- Expiration → `get` повертає `None` → confirm не виконує мутацію.
- Cancel/reject і validation error → `clear`.
- Ownership: `op.user_sub` повинен збігатися з active sub.

---

## G. Tests

```text
.\.venv\Scripts\python.exe -m pytest -q
```

**Результат:** **37 passed**, 0 failed, 0 skipped  
(попередження speech_recognition/webrtcvad — як раніше; `KeyboardInterrupt` у teardown pytest на цій машині інколи з’являється після зеленого прогону, на підрахунок passed не впливає)

Нові ключові кейси: calendar-only vs Gmail permission; incremental Gmail; invalid reschedule date/time; pending TTL; Alice/Bob isolation; LLM `user_sub` ignored.

`ruff` у проєкті не налаштований як обов’язковий gate — масово не ганявся.

---

## H. Not tested (потрібен реальний Google)

- Живий browser consent / Windows keyring persistence після reboot
- Реальні Meet links, Gmail send у прод-акаунт
- Google app verification для sensitive Gmail scopes поза Test users
- Повний голосовий e2e з мікрофоном

---

## I. Remaining risks (перед production)

1. Desktop loopback OAuth ≠ публічний web OAuth.
2. Gmail sensitive scopes потребують Google verification для широкої аудиторії.
3. Keyring fallback → in-memory при відсутності backend (токени зникають після рестарту).
4. Локальний файл `client_secret_*.json` у робочій директорії **ігнорується git**, але лежить на диску — перенесіть у `credentials/client_secret.json`; за потреби ротуйте secret у Cloud Console (значення в звіті не наводиться).
5. Немає OS-level screen lock / physical presence для `reauth_switch` — для виставкового робота варто додати пізніше PIN/UI confirm.

---

## Secrets review

| Артефакт | Статус |
|---|---|
| `.env` | у `.gitignore`, не tracked |
| `client_secret*.json` | у `.gitignore`, не tracked (`git check-ignore` OK) |
| `credentials/` | ігнорується окрім `.gitkeep` |
| `token.json` | ігнорується |
| Tracked history цього каталогу | untracked copy `voice-agent-v3-2/`; секретів у index не знайдено |

Секретні значення у цей звіт **не** включені.
