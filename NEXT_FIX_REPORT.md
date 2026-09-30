# NEXT_FIX_REPORT — виправлення аудиту Google API Voice Agent

Дата: 2026-09-29  
Проєкт: `voice-agent-v3-2`  
Python: 3.12.5 · тести: **89 passed / 0 failed / 0 skipped**

---

## 1. Причини знайдених помилок

### 1.1 Подвійне підтвердження (critical)
`PendingStore` не мав атомарного переходу `pending → executing`. Два одночасні
`confirm` могли обидва прочитати стан `pending` і двічі викликати Google API.
Після мережевої помилки з невизначеним результатом система могла дозволити
повторний «так» і створити дублікат.

### 1.2 Маршрутизація під час pending (critical)
У `AgentRouter.handle_text` будь-який текст, що не був «так»/«ні», для
calendar-pending передавався в `calendar.handle("cancel", query=raw)`. Фраза
«Дай доступ до Gmail» непомітно скасовувала/перезаписувала очікувану операцію.

### 1.3 Скасована авторизація виглядала як успіх
`AccountManager.connect` / `switch_via_reauth` повертали `AccountStatus` з
`connected=True`, якщо Alice ще була активна після скасування OAuth Bob.
Роутер трактував `connected` як успіх перемикання.

### 1.4 Scopes після restart
`Credentials.to_json()` не гарантує збереження `granted_scopes`. Підстановка
`ALL_KNOWN_SCOPES` при load могла «додати» Gmail send, якого користувач не дав.
Потрібен окремий envelope зі збереженими фактичними scopes.

### 1.5 Gmail draft / спільний пристрій
Send чернетки міг використовувати застарілий snapshot без повторного читання з
Google. Глобальний `active_sub` на спільному ПК/роботі відкривав пошту наступній
людині без нового login.

---

## 2. Змінені файли

| Файл | Зміна |
|------|--------|
| `agents/pending_store.py` | FSM: pending→executing→completed\|ambiguous\|cancelled; `begin_execute` |
| `agents/calendar_agent.py` | Atomic confirm + iCalUID idempotency + ambiguous на network |
| `agents/gmail_agent.py` | Fingerprint чернетки; granular scopes; concurrent/ambiguous protect |
| `integrations/google_calendar.py` | Fake: iCalUID + `raise_after_create` |
| `integrations/google_gmail.py` | `get_draft`; Fake: `raise_after_send` |
| `auth/token_store.py` | Envelope v2: credentials + `granted_scopes` |
| `auth/google_oauth.py` | `save_record` / load з verified scopes; refresh без інвенції scopes |
| `auth/account_manager.py` | `AuthAttemptResult`; session idle lock; granular `credentials_for` |
| `auth/scopes.py` | (вже) окремі readonly / compose / send |
| `router/agent_router.py` | Pending routing; auth_ok; lock_session |
| `router/factory.py` | `SHARED_DEVICE_MODE` / idle timeout wiring |
| `config.py` | `SHARED_DEVICE_MODE`, `SESSION_IDLE_TIMEOUT_S` |
| `tests/helpers_google.py` | FakeOAuth + `save_record` |
| `tests/test_confirm_idempotency.py` | **новий** |
| `tests/test_pending_routing.py` | **новий** |
| `tests/test_gmail_draft_and_session.py` | **новий** |
| `tests/test_oauth_accounts.py` | OAuth cancel regression |
| `tests/test_oauth_scopes.py` | readonly-only after restart |
| `tests/test_account_isolation.py` | `save_record` / unpack fix |
| `README.md`, `.env.example`, `MANUAL_GOOGLE_TESTS.md` | режими + QA |
| `TEST_RESULTS.txt`, `NEXT_FIX_REPORT.md` | цей звіт |

Секрети (`.env`, `client_secret*.json`, токени) **не** додавались у git.

---

## 3. Що змінилось у логіці

1. **Confirm:** лише один потік виграє `begin_execute`; інший отримує replay успіху
   або «вже виконується». Результат зберігається в `execution_result`.
2. **Network / timeout після мутації:** стан `ambiguous` — повторний «так» **не**
   викликає API знову; користувача просять перевірити Calendar/Sent вручну.
3. **Calendar create:** стабільний `idempotency_key` → `iCalUID` для ідемпотентного insert.
4. **Router pending:** не-так/ні → прохання завершити підтвердження; без silent cancel.
5. **OAuth:** `AuthAttemptResult.ok` окремо від `status.connected`.
6. **Scopes:** v2 envelope; Gmail readonly/compose/send перевіряються окремо.
7. **Gmail draft send:** `get_draft` + SHA-256 fingerprint до і після confirm.
8. **Session:** shared mode — idle lock / no auto-resume; personal — зберігає сесію.

---

## 4. Як запобігаємо повторним операціям

| Сценарій | Захист |
|----------|--------|
| 2× одночасний confirm | CAS `begin_execute` |
| Повторний «так» після success | `completed` + stored result, без нового API call |
| Timeout після фактичного create/send | `ambiguous`, без auto-retry |
| Calendar re-insert | той самий `iCalUID` |
| Зміна чернетки | fingerprint mismatch → block + нове confirm |

---

## 5. OAuth scopes і захист акаунтів

- При authorize: `_extract_granted_scopes` (prefer `credentials.granted_scopes`) →
  `TokenStore.save_record(..., granted_scopes)`.
- При load: **лише** збережений список, ніколи `ALL_KNOWN_SCOPES`.
- `AccountStatus`: `gmail_readonly_ready` / `compose` / `send` / `gmail_ready`.
- Голосовий switch за email відсутній; лише `reauth_switch` через браузер.
- Скасування OAuth: `auth_ok=False`, Alice може лишитись connected, але UI/голос
  повідомляє, що перемикання не відбулось.
- Shared device: `active_sub=None` після idle / lock / restart.

---

## 6. Нові / оновлені тести

- `test_confirm_idempotency.py` — concurrent confirm, reconfirm, timeout-after-API (calendar + gmail)
- `test_pending_routing.py` — «Дай доступ до Gmail» під час pending + подальше «так»
- `test_gmail_draft_and_session.py` — draft fingerprint, idle lock, restart, lock_session
- `test_oauth_accounts.py` — OAuth cancel keeps Alice, `auth_ok=False`
- `test_oauth_scopes.py` — readonly-only survives restart, no send

---

## 7. Реальні результати тестів

```
python -m pytest --collect-only -q  →  89 tests collected
python -m pytest -q                 →  89 passed, 0 failed, 0 skipped
```

Пояснення розбіжності з «37 passed» у старих звітах: suite обривався на Windows
через hang у `instance_lock` PID check; після виправлення збирається і проходить
повний набір (див. `TEST_RESULTS.txt`).

---

## 8. Що не перевірялось без реального Google

- Браузерний OAuth consent / account chooser
- Живі Calendar insert / Gmail send
- Реальна поведінка Google при частковому знятті scopes у UI consent
- Keyring на усіх ОС у production
- Фізичний робот (`go2` / `humanoid`) end-to-end

Ручний чеклист: `MANUAL_GOOGLE_TESTS.md` (додано C10–C11, G6–G7, A4–A6).

---

## 9. Залишкові ризики

1. Loopback Desktop OAuth — не для публічного web / багатокористувацького SaaS.
2. Немає OS-level screen lock / PIN перед `reauth_switch` на виставковому роботі.
3. `ambiguous` вимагає ручної перевірки користувачем — UX може плутати.
4. Google може ігнорувати/обмежувати клієнтський `iCalUID` у деяких сценаріях.
5. Shared mode не стирає refresh tokens з keyring — лише активну сесію; повний
   wipe акаунта = disconnect / revoke у Google Account.
6. Голосова модель Realtime теоретично може викликати typed tools в обхід
   free-text pending guard — typed confirm усе одно проходить через
   `begin_execute`.
