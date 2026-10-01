"""Redirect-based Google OAuth for the hosted web version.

The desktop app uses the loopback flow (InstalledAppFlow), which needs a browser on the same
machine as the Python process. On a server the user's browser is somewhere else, so Google
redirects back to `<PUBLIC_BACKEND_URL>/auth/google/callback` instead. This needs a separate
OAuth client of type "Web application" in Google Cloud Console with that redirect URI.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from auth.scopes import FULL_ACCESS_SCOPES, IDENTITY_SCOPES

# Google may return scopes in another order / with aliases (email ↔ userinfo.email).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

_PENDING_TTL_S = 15 * 60


@dataclass
class _Pending:
    client_id: str
    code_verifier: str | None
    created_at: float


class WebOAuth:
    """Starts the consent redirect and exchanges the returned code for credentials."""

    def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self._config = {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        self._redirect_uri = redirect_uri
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # local testing only

    @property
    def configured(self) -> bool:
        web = self._config["web"]
        return bool(web["client_id"] and web["client_secret"])

    def _flow(self, state: str | None = None) -> Flow:
        return Flow.from_client_config(
            self._config,
            scopes=list(IDENTITY_SCOPES + FULL_ACCESS_SCOPES),
            redirect_uri=self._redirect_uri,
            state=state,
        )

    def authorization_url(self, web_client_id: str) -> str:
        """URL of Google's consent page asking for every permission at once."""
        state = secrets.token_urlsafe(24)
        flow = self._flow(state)
        url, _ = flow.authorization_url(
            access_type="offline",  # refresh token, so the session survives the hour
            prompt="consent select_account",
            include_granted_scopes="true",
        )
        with self._lock:
            self._drop_expired()
            self._pending[state] = _Pending(web_client_id, flow.code_verifier, time.time())
        return url

    def finish(self, *, state: str, code: str) -> tuple[str, Credentials]:
        """Exchange the code. Returns (web client id that started the login, credentials)."""
        with self._lock:
            self._drop_expired()
            pending = self._pending.pop(state, None)
        if pending is None:
            raise ValueError("unknown_or_expired_state")
        flow = self._flow(state)
        flow.code_verifier = pending.code_verifier
        flow.fetch_token(code=code)
        return pending.client_id, flow.credentials

    def _drop_expired(self) -> None:
        cutoff = time.time() - _PENDING_TTL_S
        for key in [k for k, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[key]
