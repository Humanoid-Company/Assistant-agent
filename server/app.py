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
from contextlib import asynccontextmanager
from datetime import date

import httpx
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
    OPENAI_LIVE_BACKEND_EFFORT,
    OPENAI_LIVE_BACKEND_MODEL,
    OPENAI_LIVE_MODEL,
    OPENAI_LIVE_PARALLEL_TOOLS,
    OPENAI_LIVE_VOICE,
)
from prompts.backend_prompt import build_backend_prompt
from prompts.live_prompt import build_live_prompt
from server.live_bridge import SidebandToolBridge
from server.web_users import WebUser, WebUserRegistry
from tools.live_schemas import LIVE_BACKEND_TOOLS
from voice.delegation import responses_delegation
from voice.options import LANGUAGE_OPTIONS, SPEED_OPTIONS, STYLE_OPTIONS, VOICE_PERSONAS, delivery_instruction

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("server")

PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000").rstrip("/")
FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "*").split(",") if o.strip()]
ACCESS_CODE = os.getenv("ACCESS_CODE", "").strip()
# Hard cap on one call's length (cost guard); the page also ends a call after 5 min of silence.
MAX_SESSION_S = float(os.getenv("MAX_SESSION_MINUTES", "30")) * 60

_WEB_NOTE = (
    "\nWeb demo: you run in a browser tab for the team to try. To connect Google the person "
    "presses the «Підключити Google» button on the page; you will be told when the login finishes. "
    "There are no local busy cues here: before a task that takes a moment (calendar, mail, notes, "
    "web search) say a very short acknowledgement first — «Так», «Зараз гляну», «Секунду» — vary it."
)
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")
_background_tasks: set[asyncio.Task] = set()

# Free Render sleeps after ~15 min without inbound requests, and a restart forgets everyone's
# Google login. While running, ping our own public URL (through Render's proxy, so it counts as
# inbound traffic). It can't wake a sleeping server — the first visitor does that. 0 = off.
# Note: one always-on free service uses ~744 of the 750 free instance hours a month.
KEEP_AWAKE_S = float(os.getenv("KEEP_AWAKE_MINUTES", "10")) * 60


async def _keep_awake() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            await asyncio.sleep(KEEP_AWAKE_S)
            try:
                await http.get(f"{PUBLIC_BACKEND_URL}/healthz")
            except httpx.HTTPError:
                logger.warning("keep-awake ping failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    task = None
    if KEEP_AWAKE_S > 0 and PUBLIC_BACKEND_URL.startswith("https://"):
        task = asyncio.create_task(_keep_awake())
        logger.info("keep-awake every %ss → %s/healthz", int(KEEP_AWAKE_S), PUBLIC_BACKEND_URL)
    yield
    if task is not None:
        task.cancel()


app = FastAPI(title="Voice agent web backend", lifespan=_lifespan)
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


def _user_voice(user: WebUser) -> str:
    return user.voice or OPENAI_LIVE_VOICE


def _session_config(user: WebUser) -> dict:
    language = LANGUAGE_OPTIONS.get(user.language, LANGUAGE_OPTIONS["uk"])
    today = date.today().isoformat()
    voice = _user_voice(user)
    config = {
        "model": OPENAI_LIVE_MODEL,
        "instructions": build_live_prompt(
            language_name=language,
            assistant_name=user.assistant_name,
            today=today,
            voice=voice,
            delivery=delivery_instruction(user.speed, user.style),
        )
        + _WEB_NOTE,
        "audio": {"output": {"voice": voice}},
        "delegation": responses_delegation(
            model=OPENAI_LIVE_BACKEND_MODEL,
            instructions=build_backend_prompt(today=today, language_name=language),
            tools=LIVE_BACKEND_TOOLS,
            parallel_tools=OPENAI_LIVE_PARALLEL_TOOLS,
            effort=OPENAI_LIVE_BACKEND_EFFORT,
        ),
    }
    # A new voice or a wake after a long pause is a new Live session: it starts from the
    # conversation so far, so nothing said before is forgotten.
    history = user.conversation.live_input()
    if history:
        config["input"] = history
    return config


class SessionRequest(BaseModel):
    sdp: str
    # The page's picker choices: server state is in memory and is lost on a Render restart.
    voice: str | None = None
    speed: str | None = None
    style: str | None = None


class VoiceRequest(BaseModel):
    voice: str | None = None
    speed: str | None = None
    style: str | None = None


@app.get("/healthz")
def healthz() -> dict:
    # Render sets these: shows which commit/branch is actually deployed.
    return {
        "ok": True,
        "commit": os.getenv("RENDER_GIT_COMMIT", "")[:7] or None,
        "branch": os.getenv("RENDER_GIT_BRANCH") or None,
    }


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
    if body.voice and body.voice.strip().lower() in VOICE_PERSONAS:
        user.voice = body.voice.strip().lower()
    if body.speed in SPEED_OPTIONS:
        user.speed = body.speed
    if body.style in STYLE_OPTIONS:
        user.style = body.style
    user.reconnect_pending = False
    try:
        result = await openai_client.live.create(
            session=_session_config(user), transport={"type": "webrtc", "sdp": body.sdp}
        )
    except Exception as exc:
        logger.exception("web.session.create_failed")
        raise HTTPException(status_code=502, detail=f"live_create_failed: {type(exc).__name__}") from exc
    session_id = result.session.id
    # One call per browser: an older session (another tab, a page reload) would keep billing
    # and run tools against the same account in parallel.
    for old in list(user.bridges):
        try:
            await old.close()
        except Exception:
            logger.debug("closing old session failed", exc_info=True)
    bridge = SidebandToolBridge(
        client=openai_client,
        session_id=session_id,
        executor=user.executor,
        on_closed=user.bridges.discard,
        max_duration_s=MAX_SESSION_S,
        conversation=user.conversation,
        on_voice_request=user.switch_voice_by_request,
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
        "voice": user.voice,  # None until chosen; the page keeps its own choice then
        "speed": user.speed,
        "style": user.style,
        "reconnect": user.reconnect_pending,  # voice changed by voice command: page reconnects
        "history_turns": len(user.conversation),
    }


@app.get("/api/voices")
def voices() -> dict:
    """Voices for the page's picker; the chosen one applies from the next call."""
    return {
        "default": OPENAI_LIVE_VOICE,
        "voices": [
            {"id": p.voice, "label": p.label, "description": p.description, "feminine": p.feminine}
            for p in VOICE_PERSONAS.values()
        ],
    }


@app.post("/api/voice")
def set_voice(
    body: VoiceRequest,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """Voice (applies via reconnect) and speed/style. Both are applied by the page: it reconnects
    for a voice, and sends the returned speed/style instruction once Єва is quiet — the page hears
    her audio, while an instruction that lands mid-answer derails it."""
    user = _user(x_client_id, x_access_code)
    if body.voice is not None:
        voice = body.voice.strip().lower()
        if voice not in VOICE_PERSONAS:
            raise HTTPException(status_code=400, detail="unknown_voice")
        user.voice = voice
    delivery_changed = False
    for name, options in (("speed", SPEED_OPTIONS), ("style", STYLE_OPTIONS)):
        value = getattr(body, name)
        if value is None:
            continue
        if value not in options:
            raise HTTPException(status_code=400, detail=f"unknown_{name}")
        delivery_changed |= value != getattr(user, name)
        setattr(user, name, value)
    instruction = delivery_instruction(user.speed, user.style, changed=True) if delivery_changed else ""
    return {"voice": user.voice, "speed": user.speed, "style": user.style, "instruction": instruction}


class VoiceRequestBody(BaseModel):
    text: str


@app.post("/api/voice-request")
def voice_request(
    body: VoiceRequestBody,
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """«Єва, зміни голос на …» heard by the browser's recogniser — faster than the Live transcript.
    The page restarts the call in the new voice when switched is true."""
    user = _user(x_client_id, x_access_code)
    switched = user.switch_voice_by_request(body.text[:500])
    if switched:
        user.reconnect_pending = False  # the page reconnects itself
    return {"switched": switched, "voice": user.voice}


@app.delete("/api/conversation")
def clear_conversation(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """«Нова розмова» / another person signed in: forget the dialogue history."""
    _user(x_client_id, x_access_code).conversation.clear()
    return {"ok": True}


@app.post("/api/google/disconnect")
def google_disconnect(
    x_client_id: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id, x_access_code)
    user.conversation.clear()  # the next person must not inherit this conversation
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
