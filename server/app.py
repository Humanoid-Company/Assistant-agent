"""HTTP backend for the hosted web version (deploy on Render).

    uvicorn server.app:app --host 0.0.0.0 --port 8000

The browser (web/, deployed on Vercel) talks to OpenAI GPT-Live directly over WebRTC for
audio; this server only:
  - creates the Live session from the browser's SDP offer (the OpenAI key never leaves it),
  - attaches a sideband to that session and runs the tools (Google, web search, …),
  - does the Google sign-in redirect flow.

Env: OPENAI_API_KEY, PUBLIC_BACKEND_URL, FRONTEND_ORIGINS, GOOGLE_OAUTH_WEB_CLIENT_ID,
GOOGLE_OAUTH_WEB_CLIENT_SECRET, optional ACCESS_CODE, WEB_SEARCH_API_KEY. See DEPLOY.md.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import date

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from openai import AsyncOpenAI
from pydantic import BaseModel

from auth.google_oauth import GoogleOAuthClient
from auth.token_store import InMemoryTokenStore
from auth.web_oauth import WebOAuth
from config import (
    GOOGLE_CALENDAR_TIMEZONE,
    GOOGLE_OAUTH_CLIENT_SECRETS_FILE,
    OPENAI_API_KEY,
    OPENAI_LIVE_BACKEND_MODEL,
    OPENAI_LIVE_MODEL,
    OPENAI_LIVE_VOICE,
)
from prompts.backend_prompt import build_backend_prompt
from prompts.live_prompt import build_live_prompt
from server.live_bridge import SidebandToolBridge
from server.web_users import WebUser, WebUserRegistry
from tools.live_schemas import LIVE_BACKEND_TOOLS
from voice.options import LANGUAGE_OPTIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("server")

PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000").rstrip("/")
FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "*").split(",") if o.strip()]
ACCESS_CODE = os.getenv("ACCESS_CODE", "").strip()

_WEB_NOTE = (
    "\nWeb demo: you run in a browser tab for the team to try — there is no physical robot body "
    "here, so don't offer to move. To connect Google the person presses the «Підключити Google» "
    "button on the page; you will be told when the login finishes."
)
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")
_background_tasks: set[asyncio.Task] = set()

app = FastAPI(title="Voice agent web backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
users = WebUserRegistry(client_secrets_file=GOOGLE_OAUTH_CLIENT_SECRETS_FILE, timezone=GOOGLE_CALENDAR_TIMEZONE)
web_oauth = WebOAuth(
    client_id=os.getenv("GOOGLE_OAUTH_WEB_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_OAUTH_WEB_CLIENT_SECRET", ""),
    redirect_uri=f"{PUBLIC_BACKEND_URL}/auth/google/callback",
)
# Only used to read the signed-in user's Google identity; never runs the desktop flow here.
_identity_reader = GoogleOAuthClient(GOOGLE_OAUTH_CLIENT_SECRETS_FILE, InMemoryTokenStore())


def _user(client_id: str | None, access_code: str | None) -> WebUser:
    if ACCESS_CODE and (access_code or "") != ACCESS_CODE:
        raise HTTPException(status_code=401, detail="access_code_required")
    if not client_id or not _CLIENT_ID_RE.match(client_id):
        raise HTTPException(status_code=400, detail="bad_client_id")
    return users.get(client_id)


def _session_config(user: WebUser) -> dict:
    language = LANGUAGE_OPTIONS.get(user.language, LANGUAGE_OPTIONS["uk"])
    today = date.today().isoformat()
    return {
        "model": OPENAI_LIVE_MODEL,
        "instructions": build_live_prompt(
            language_name=language, assistant_name=user.assistant_name, today=today
        )
        + _WEB_NOTE,
        "audio": {"output": {"voice": user.voice or OPENAI_LIVE_VOICE}},
        "delegation": {
            "type": "responses",
            "responses": {
                "model": OPENAI_LIVE_BACKEND_MODEL,
                "instructions": build_backend_prompt(today=today, language_name=language),
                "tools": LIVE_BACKEND_TOOLS,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            },
        },
    }


class SessionRequest(BaseModel):
    sdp: str


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/config")
def public_config() -> dict:
    """What the page needs to know before anything else."""
    return {"access_code_required": bool(ACCESS_CODE), "google_login": web_oauth.configured}


@app.post("/api/session")
async def create_session(
    body: SessionRequest,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """Browser SDP offer in → SDP answer out; tools run here over a sideband."""
    user = _user(x_client_id, x_access_code)
    try:
        result = await openai_client.live.create(
            session=_session_config(user), transport={"type": "webrtc", "sdp": body.sdp}
        )
    except Exception as exc:
        logger.exception("web.session.create_failed")
        raise HTTPException(status_code=502, detail=f"live_create_failed: {type(exc).__name__}") from exc
    session_id = result.session.id
    bridge = SidebandToolBridge(
        client=openai_client,
        session_id=session_id,
        executor=user.executor,
        on_closed=user.bridges.discard,
    )
    user.bridges.add(bridge)
    task = asyncio.create_task(bridge.run())
    _background_tasks.add(task)  # keep a reference so the task isn't garbage-collected
    task.add_done_callback(_background_tasks.discard)
    logger.info("web.session.created session_id=%s", session_id)
    return {"sdp": result.transport.sdp, "session_id": session_id}


@app.get("/api/me")
def me(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id, x_access_code)
    status = user.router.google_status()
    data = status.data or {}
    return {
        "google": {
            "connected": bool(data.get("connected")),
            "email": data.get("email"),
            "calendar": bool(data.get("calendar_ready")),
            "gmail": bool(data.get("gmail_ready")),
            "notes": bool(data.get("notes_ready")),
        },
        "assistant_name": user.assistant_name,
    }


@app.post("/api/google/disconnect")
def google_disconnect(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id, x_access_code)
    return {"message": user.router.disconnect_google().message}


@app.get("/auth/google/start")
def google_start(client_id: str = Query(...), access_code: str | None = Query(default=None)):
    """Opened by the page in a popup: redirect to Google's consent screen."""
    _user(client_id, access_code)
    if not web_oauth.configured:
        return _page("Вхід у Google не налаштовано на сервері (GOOGLE_OAUTH_WEB_CLIENT_ID/SECRET).", ok=False)
    return RedirectResponse(web_oauth.authorization_url(client_id))


@app.get("/auth/google/callback")
async def google_callback(request: Request):
    params = request.query_params
    if params.get("error"):
        return _page("Вхід скасовано або відхилено. Можна закрити це вікно й спробувати знову.", ok=False)
    state, code = params.get("state"), params.get("code")
    if not state or not code:
        return _page("Некоректна відповідь Google.", ok=False)
    try:
        client_id, credentials = await asyncio.to_thread(web_oauth.finish, state=state, code=code)
        identity = await asyncio.to_thread(_identity_reader.fetch_identity, credentials)
    except Exception:
        logger.exception("web.google.callback_failed")
        return _page("Не вдалося завершити вхід у Google. Спробуйте ще раз.", ok=False)
    user = users.get(client_id)
    granted = list(getattr(credentials, "granted_scopes", None) or credentials.scopes or [])
    status = await asyncio.to_thread(
        user.router.accounts.activate, identity, credentials_json=credentials.to_json(), granted=granted
    )
    for bridge in list(user.bridges):  # tell an ongoing conversation right away
        try:
            await bridge.say(status.message)
        except Exception:
            logger.debug("announce failed", exc_info=True)
    logger.info("web.google.connected sub=%s…", identity.sub[:8])
    return _page(status.message, ok=True)


def _page(message: str, *, ok: bool) -> HTMLResponse:
    """Small page shown in the login popup; it notifies the main page and closes itself."""
    safe = html.escape(message)
    body = f"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Google</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 16px;
line-height:1.5;color:#1d1d1f;background:#fafafa}}</style></head><body>
<p>{"✅" if ok else "⚠️"} {safe}</p><p>Це вікно можна закрити.</p>
<script>try{{window.opener&&window.opener.postMessage({{type:"google-login",ok:{str(ok).lower()}}},"*")}}catch(e){{}}
setTimeout(function(){{window.close()}},1500)</script></body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)
