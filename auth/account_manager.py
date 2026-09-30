"""Active Google account selection — voice/email is never an authentication factor."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from google.oauth2.credentials import Credentials

from auth.google_oauth import GoogleIdentity, GoogleOAuthClient, OAuthError, granted_scopes
from auth.scopes import (
    CALENDAR_SCOPES,
    GMAIL_COMPOSE_SCOPES,
    GMAIL_READONLY_SCOPES,
    GMAIL_SCOPES,
    GMAIL_SEND_SCOPES,
    IDENTITY_SCOPES,
    NOTES_SCOPES,
)
from auth.token_store import TokenStore

logger = logging.getLogger(__name__)


@dataclass
class AccountStatus:
    connected: bool
    active_sub: str | None
    email: str | None
    name: str | None
    calendar_ready: bool
    gmail_readonly_ready: bool
    gmail_compose_ready: bool
    gmail_send_ready: bool
    gmail_ready: bool
    notes_ready: bool = False
    granted_scopes: list[str] = field(default_factory=list)
    accounts: list[dict[str, str]] = field(default_factory=list)
    message: str = ""
    # Separates "someone is still connected" from "the latest auth attempt succeeded".
    last_auth_ok: bool | None = None
    session_locked: bool = False


@dataclass
class AuthAttemptResult:
    ok: bool
    status: AccountStatus
    message: str


class AccountManager:
    """Owns the active Google user (`sub`). LLM/tool args never choose credentials.

    Modes (see config.SHARED_DEVICE_MODE):
    - personal desktop: active_sub persists; idle lock optional / disabled.
    - shared device / robot: idle timeout clears active session so the next
      person cannot silently use the previous mailbox.
    """

    def __init__(
        self,
        oauth: GoogleOAuthClient,
        token_store: TokenStore,
        state_file: Path,
        *,
        shared_device: bool = False,
        session_idle_timeout_s: float = 300.0,
    ) -> None:
        self._oauth = oauth
        self._store = token_store
        self._state_file = Path(state_file)
        self._lock = threading.RLock()
        self._active_sub: str | None = None
        self._profiles: dict[str, dict[str, str]] = {}
        self._shared_device = shared_device
        self._session_idle_timeout_s = max(0.0, float(session_idle_timeout_s))
        self._last_activity_at: float = time.time()
        self._session_locked = False
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._active_sub = data.get("active_sub")
        self._profiles = data.get("profiles") or {}
        # On shared devices, never auto-resume a previous user's session after restart.
        if self._shared_device:
            self._active_sub = None
            self._session_locked = True

    def _save_state(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"active_sub": self._active_sub, "profiles": self._profiles}
        self._state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _session_touch(self) -> None:
        self._last_activity_at = time.time()
        self._session_locked = False

    def _enforce_idle_lock(self) -> None:
        if not self._shared_device or self._session_idle_timeout_s <= 0:
            return
        if not self._active_sub or self._session_locked:
            return
        if (time.time() - self._last_activity_at) >= self._session_idle_timeout_s:
            logger.info("Shared-device idle timeout — locking Google session")
            self._active_sub = None
            self._session_locked = True
            self._save_state()

    def lock_session(self) -> AccountStatus:
        """Explicitly clear the active Google session (shared PC / robot hand-off)."""
        with self._lock:
            self._active_sub = None
            self._session_locked = True
            self._save_state()
            st = self.status()
            st.message = "Сесію Google заблоковано. Наступному користувачу потрібен новий вхід у браузері."
            st.session_locked = True
            return st

    def active_sub(self) -> str | None:
        with self._lock:
            self._enforce_idle_lock()
            return self._active_sub

    def status(self) -> AccountStatus:
        with self._lock:
            self._enforce_idle_lock()
            accounts = [
                {
                    "sub": sub,
                    "email": self._profiles.get(sub, {}).get("email", ""),
                    "name": self._profiles.get(sub, {}).get("name", ""),
                }
                for sub in self._store.list_subs()
            ]
            empty_msg = (
                "Сесію заблоковано через бездіяльність або ручне блокування. "
                "Скажіть «підключи Google», щоб увійти через браузер."
                if self._session_locked
                else "Google-акаунт не підключено. Скажіть «підключи Google», щоб увійти через браузер."
            )
            empty = AccountStatus(
                connected=False,
                active_sub=None,
                email=None,
                name=None,
                calendar_ready=False,
                gmail_readonly_ready=False,
                gmail_compose_ready=False,
                gmail_send_ready=False,
                gmail_ready=False,
                notes_ready=False,
                granted_scopes=[],
                accounts=accounts,
                message=empty_msg,
                session_locked=self._session_locked,
            )
            if not self._active_sub or self._active_sub not in self._store.list_subs():
                return empty

            profile = self._profiles.get(self._active_sub, {})
            try:
                creds = self._oauth.load_credentials(self._active_sub)
            except OAuthError as exc:
                return AccountStatus(
                    connected=True,
                    active_sub=self._active_sub,
                    email=profile.get("email"),
                    name=profile.get("name"),
                    calendar_ready=False,
                    gmail_readonly_ready=False,
                    gmail_compose_ready=False,
                    gmail_send_ready=False,
                    gmail_ready=False,
                    notes_ready=False,
                    granted_scopes=[],
                    accounts=accounts,
                    message=str(exc),
                    session_locked=False,
                )

            scopes = granted_scopes(creds) if creds else []
            calendar_ready = bool(creds) and self._oauth.has_scopes(creds, CALENDAR_SCOPES)
            gmail_ro = bool(creds) and self._oauth.has_scopes(creds, GMAIL_READONLY_SCOPES)
            gmail_compose = bool(creds) and self._oauth.has_scopes(creds, GMAIL_COMPOSE_SCOPES)
            gmail_send = bool(creds) and self._oauth.has_scopes(creds, GMAIL_SEND_SCOPES)
            gmail_ready = gmail_ro and gmail_compose and gmail_send
            notes_ready = bool(creds) and self._oauth.has_scopes(creds, NOTES_SCOPES)
            email = profile.get("email") or ""
            return AccountStatus(
                connected=True,
                active_sub=self._active_sub,
                email=email or None,
                name=profile.get("name"),
                calendar_ready=calendar_ready,
                gmail_readonly_ready=gmail_ro,
                gmail_compose_ready=gmail_compose,
                gmail_send_ready=gmail_send,
                gmail_ready=gmail_ready,
                notes_ready=notes_ready,
                granted_scopes=scopes,
                accounts=accounts,
                message=(
                    f"Підключено {email or 'акаунт'}. "
                    f"Календар: {'так' if calendar_ready else 'ні'}. "
                    f"Gmail читання: {'так' if gmail_ro else 'ні'}. "
                    f"Gmail надсилання: {'так' if gmail_send else 'ні'}. "
                    f"Нотатки: {'так' if notes_ready else 'ні'}."
                ),
                session_locked=False,
            )

    def connect(self, *, with_calendar: bool = True, with_gmail: bool = False) -> AuthAttemptResult:
        """Interactive login. On cancel, previous account stays — but ok=False."""
        with self._lock:
            scopes = list(IDENTITY_SCOPES)
            if with_calendar:
                scopes.extend(CALENDAR_SCOPES)
            if with_gmail:
                scopes.extend(GMAIL_SCOPES)
            try:
                identity, _ = self._oauth.authorize(scopes=scopes)
            except OAuthError as exc:
                st = self.status()
                st.last_auth_ok = False
                st.message = str(exc)
                if st.connected:
                    st.message = (
                        f"{exc}. Попередній акаунт ({st.email or st.active_sub}) лишається активним — "
                        "перемикання не відбулось."
                    )
                return AuthAttemptResult(ok=False, status=st, message=st.message)
            self._profiles[identity.sub] = {"email": identity.email, "name": identity.name}
            self._active_sub = identity.sub
            self._session_touch()
            self._save_state()
            st = self.status()
            st.last_auth_ok = True
            return AuthAttemptResult(ok=True, status=st, message=st.message)

    def request_gmail_permission(self) -> AuthAttemptResult:
        with self._lock:
            if not self._active_sub:
                raise OAuthError("not_connected", "Спочатку підключіть Google-акаунт через браузер.")
            try:
                identity, _ = self._oauth.request_scopes(self._active_sub, GMAIL_SCOPES)
            except OAuthError as exc:
                st = self.status()
                st.last_auth_ok = False
                st.message = str(exc)
                return AuthAttemptResult(ok=False, status=st, message=st.message)
            self._profiles[identity.sub] = {"email": identity.email, "name": identity.name}
            self._session_touch()
            self._save_state()
            st = self.status()
            st.last_auth_ok = True
            if st.gmail_ready:
                st.message = (
                    "Дозвіл Gmail надано (permission_granted). "
                    "Можна одразу шукати й читати пошту — повторна авторизація не потрібна."
                )
            logger.info(
                "google.permission.granted sub=%s… gmail_ready=%s",
                identity.sub[:8],
                st.gmail_ready,
            )
            return AuthAttemptResult(ok=True, status=st, message=st.message)

    def request_calendar_permission(self) -> AuthAttemptResult:
        with self._lock:
            if not self._active_sub:
                raise OAuthError("not_connected", "Спочатку підключіть Google-акаунт через браузер.")
            try:
                identity, _ = self._oauth.request_scopes(self._active_sub, CALENDAR_SCOPES)
            except OAuthError as exc:
                st = self.status()
                st.last_auth_ok = False
                st.message = str(exc)
                return AuthAttemptResult(ok=False, status=st, message=st.message)
            self._profiles[identity.sub] = {"email": identity.email, "name": identity.name}
            self._session_touch()
            self._save_state()
            st = self.status()
            st.last_auth_ok = True
            return AuthAttemptResult(ok=True, status=st, message=st.message)

    def request_notes_permission(self) -> AuthAttemptResult:
        with self._lock:
            if not self._active_sub:
                raise OAuthError("not_connected", "Спочатку підключіть Google-акаунт через браузер.")
            try:
                identity, _ = self._oauth.request_scopes(self._active_sub, NOTES_SCOPES)
            except OAuthError as exc:
                st = self.status()
                st.last_auth_ok = False
                st.message = str(exc)
                return AuthAttemptResult(ok=False, status=st, message=st.message)
            self._profiles[identity.sub] = {"email": identity.email, "name": identity.name}
            self._session_touch()
            self._save_state()
            st = self.status()
            st.last_auth_ok = True
            if st.notes_ready:
                st.message = (
                    "Дозвіл на нотатки Google Drive надано (permission_granted). "
                    "Можна одразу записувати й читати нотатки — повторна авторизація не потрібна."
                )
            logger.info(
                "google.permission.granted sub=%s… notes_ready=%s",
                identity.sub[:8],
                st.notes_ready,
            )
            return AuthAttemptResult(ok=True, status=st, message=st.message)

    def disconnect(self, google_sub: str | None = None) -> AccountStatus:
        with self._lock:
            target = google_sub or self._active_sub
            if not target:
                return self.status()
            # Only allow disconnect of the *active* account via voice/tools —
            # never pass an arbitrary sub from the LLM.
            if google_sub is not None and google_sub != self._active_sub:
                raise OAuthError(
                    "forbidden_switch",
                    "Можна відключити лише активний акаунт. Інший акаунт голосом не обирається.",
                )
            self._store.delete(target)
            self._profiles.pop(target, None)
            self._active_sub = None
            self._session_locked = False
            self._save_state()
            st = self.status()
            st.message = "Акаунт відключено."
            return st

    def switch_via_reauth(self) -> AuthAttemptResult:
        """Change active account only through a fresh browser OAuth (account chooser).

        Spoken email is NOT accepted — that would let anyone on a shared robot
        access a previously authorized mailbox by naming it.
        """
        return self.connect(with_calendar=True, with_gmail=False)

    def require_active_sub(self) -> str:
        with self._lock:
            self._enforce_idle_lock()
            if not self._active_sub:
                raise OAuthError("not_connected", "Спочатку підключіть Google-акаунт через браузер.")
            self._session_touch()
            return self._active_sub

    def credentials_for(
        self,
        *,
        calendar: bool = False,
        gmail: bool = False,
        gmail_readonly: bool = False,
        gmail_compose: bool = False,
        gmail_send: bool = False,
        notes: bool = False,
    ) -> tuple[str, Credentials]:
        """Credentials for the trusted active session only — ignores any LLM-supplied identity."""
        with self._lock:
            sub = self.require_active_sub()
            required: list[str] = list(IDENTITY_SCOPES)
            if calendar:
                required.extend(CALENDAR_SCOPES)
            if gmail:
                required.extend(GMAIL_SCOPES)
            else:
                if gmail_readonly:
                    required.extend(GMAIL_READONLY_SCOPES)
                if gmail_compose:
                    required.extend(GMAIL_COMPOSE_SCOPES)
                if gmail_send:
                    required.extend(GMAIL_SEND_SCOPES)
            if notes:
                required.extend(NOTES_SCOPES)
            credentials = self._oauth.require_scopes(sub, required)
            return sub, credentials

    def remember_profile(self, identity: GoogleIdentity) -> None:
        with self._lock:
            self._profiles[identity.sub] = {"email": identity.email, "name": identity.name}
            self._save_state()
