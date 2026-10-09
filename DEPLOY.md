# Веб-версія для команди: Vercel (фронт) + Render (бек)

## Як це влаштовано

```
Браузер (web/, Vercel) ──WebRTC: мікрофон і звук──► OpenAI GPT-Live
        │                                                ▲
        │ SDP-оффер, вхід у Google                       │ sideband (інструменти)
        ▼                                                │
Бекенд (server/, Render) ────────────────────────────────┘
   └─ Google Calendar / Gmail / нотатки, вебпошук
```

- **Звук іде напряму між браузером і OpenAI.** Затримка мінімальна, а браузер сам придушує ехо (`echoCancellation`), тож агент не перебиває сам себе.
- **Ключ OpenAI лише на бекенді.** Браузер надсилає свій SDP, бекенд створює сесію й повертає SDP-відповідь.
- **Інструменти виконує бекенд.** Він під'єднується до тієї ж сесії через sideband і викликає Google API та вебпошук.
- **У кожного браузера свій Google-акаунт.** Його розрізняє випадковий id у `localStorage`.
- **Дані Google зберігаються лише в пам'яті сервера.** Після рестарту на Render треба підключитись знову.

## 1. Google Cloud: OAuth-клієнт для вебу (один раз)

Десктопний клієнт (`credentials/client_secret.json`) на сервері **не працює**: потрібен окремий клієнт.

1. Google Cloud Console → **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Тип: **Web application**.
3. **Authorized redirect URIs**: `https://<ваш-сервіс>.onrender.com/auth/google/callback`. Точну адресу видно після кроку 2. Можна додати й `http://localhost:8000/auth/google/callback` для локального тесту.
4. Збережіть **Client ID** і **Client secret**.
5. **OAuth consent screen → Test users**: додайте Gmail кожного, хто тестуватиме. Поки застосунок у режимі Testing, інші акаунти отримають «Доступ заблоковано».
   - Обмеження Google: у режимі Testing доступ до акаунта діє 7 днів, потім треба підключитись знову.

## 2. Render: бекенд

1. Render → **New → Blueprint** → цей репозиторій. Render підхопить `render.yaml`.
   - Або вручну: **New → Web Service**, Runtime **Python**:
     - Build: `pip install -r requirements-server.txt`
     - Start: `uvicorn server.app:app --host 0.0.0.0 --port $PORT`
     - Env: `PYTHON_VERSION=3.12.7`
2. Змінні середовища:

| Змінна | Значення |
|---|---|
| `OPENAI_API_KEY` | ключ OpenAI |
| `PUBLIC_BACKEND_URL` | `https://<ваш-сервіс>.onrender.com` (без `/` у кінці) |
| `FRONTEND_ORIGINS` | адреса з Vercel, напр. `https://voice-agent.vercel.app` (кілька — через кому) |
| `GOOGLE_OAUTH_WEB_CLIENT_ID` | з кроку 1 |
| `GOOGLE_OAUTH_WEB_CLIENT_SECRET` | з кроку 1 |
| `ACCESS_CODE` | пароль для команди. **Задайте обов'язково:** без нього будь-хто з посиланням витрачатиме ваш баланс OpenAI |
| `WEB_SEARCH_API_KEY` | ключ Tavily (необов'язково) |
| `ELEVENLABS_API_KEY` | ключ ElevenLabs для тестової сторінки `/eleven` (необов'язково; без нього вона вимкнена). `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` — голос і модель за замовчуванням |

3. Перевірка: `https://<ваш-сервіс>.onrender.com/healthz` → `{"ok":true}`.

> **Free-план Render засинає** після ~15 хв без запитів, перше відкриття сторінки «будить» його ~30–60 с. Для демо краще план Starter.

## 3. Vercel: фронт

1. У `web/config.js` впишіть адресу бекенду: `BACKEND_URL: "https://<ваш-сервіс>.onrender.com"`. Закомітьте.
2. Vercel → **Add New → Project** → цей репозиторій.
   - **Root Directory: `web`**
   - Framework Preset: **Other**, Build Command порожній, Output Directory порожній (статичні файли).
3. Після деплою скопіюйте адресу (`https://….vercel.app`) у `FRONTEND_ORIGINS` на Render і перезапустіть сервіс.

Разова перевірка з іншим бекендом без редеплою: `https://….vercel.app/?backend=https://інший.onrender.com`.

## 4. Як тестувати

1. Відкрити сторінку → ввести код доступу (якщо задано).
2. **«Підключити Google»** → у вікні Google обрати акаунт і поставити всі галочки. Якщо Google попереджає «додаток не перевірено»: «Додатково» → «Перейти до …».
3. **«Почати розмову»** → дозволити мікрофон → говорити.

## Локальний запуск веб-версії

```bash
uv sync --group server
# .env: OPENAI_API_KEY, PUBLIC_BACKEND_URL=http://localhost:8000,
#       GOOGLE_OAUTH_WEB_CLIENT_ID/SECRET (redirect http://localhost:8000/auth/google/callback)
.venv/Scripts/python.exe -m uvicorn server.app:app --port 8000
# фронт: будь-який статичний сервер з папки web/, напр.
python -m http.server 5173 -d web   # і відкрити http://localhost:5173/?backend=http://localhost:8000
```

## Що відрізняється від десктопної версії

| Десктоп (`main.py`) | Веб |
|---|---|
| wake word «привіт» | кнопка «Почати розмову» |
| локальний мікрофон, VAD і власний захист від ехо | браузер: WebRTC + вбудоване придушення ехо |
| вхід у Google через loopback-вікно на тому ж ПК | вхід через редірект на бекенд (Web OAuth-клієнт) |
| токени в Windows Credential Manager | токени в пам'яті сервера (зникають після рестарту) |
| зміна голосу — одразу | з наступної розмови |
