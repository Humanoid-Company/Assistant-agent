"""Desktop OAuth 2.0 (InstalledAppFlow + loopback). Local MVP only — not a public web OAuth solution."""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from auth.scopes import IDENTITY_SCOPES, scope_labels
from auth.token_store import TokenStore, TokenStoreError

logger = logging.getLogger(__name__)

# How long the browser consent may stay open before the attempt is given up.
OAUTH_CONSENT_TIMEOUT_S = int(os.getenv("OAUTH_CONSENT_TIMEOUT_S", "600"))


class OAuthError(Exception):
    """User-facing OAuth failure (denied consent, missing client secrets, revoked token)."""

    def __init__(self, code: str, message: str, *, missing_scopes: list[str] | None = None) -> None:
        self.code = code
        self.missing_scopes = missing_scopes or []
        super().__init__(message)


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str


FlowFactory = Callable[[str, list[str]], InstalledAppFlow]


def _default_flow_factory(client_secrets_file: str, scopes: list[str]) -> InstalledAppFlow:
    return InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes)


def granted_scopes(credentials: Credentials, *, explicit: list[str] | None = None) -> list[str]:
    """Prefer explicitly persisted granted_scopes; fall back to credentials.scopes."""
    if explicit is not None:
        return list(explicit)
    return list(credentials.scopes or [])


class GoogleOAuthClient:
    """Runs the Desktop OAuth loopback flow and refreshes stored credentials."""

    def __init__(
        self,
        client_secrets_file: Path | str,
        token_store: TokenStore,
        *,
        flow_factory: FlowFactory | None = None,
        open_browser: bool = True,
    ) -> None:
        self._client_secrets = Path(client_secrets_file)
        self._store = token_store
        self._flow_factory = flow_factory or _default_flow_factory
        self._open_browser = open_browser
        # One live Credentials object per account while its stored record is unchanged: the
        # pooled Google connections hold it and refresh it in place, so a new copy per tool
        # call would refresh the same expired token a second time.
        self._live: dict[str, tuple[tuple[str, tuple[str, ...]], Credentials]] = {}
        self._live_lock = threading.Lock()

    def ensure_client_secrets(self) -> None:
        if not self._client_secrets.exists():
            raise OAuthError(
                "missing_client_secrets",
                "Не знайдено файл OAuth Desktop-клієнта. Покладіть client_secret.json "
                f"за шляхом {self._client_secrets} (див. README).",
            )
        try:
            data = json.loads(self._client_secrets.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OAuthError("invalid_client_secrets", "Файл OAuth-клієнта пошкоджений.") from exc
        if "installed" not in data:
            raise OAuthError(
                "wrong_client_type",
                "Потрібен OAuth client типу Desktop app (ключ 'installed' у JSON), не Web.",
            )

    def authorize(self, scopes: tuple[str, ...] | list[str] | None = None) -> tuple[GoogleIdentity, Credentials]:
        """Interactive browser consent. Returns verified identity + credentials with *granted* scopes only."""
        self.ensure_client_secrets()
        wanted = list(scopes or list(IDENTITY_SCOPES))
        for scope in IDENTITY_SCOPES:
            if scope not in wanted:
                wanted.append(scope)
        try:
            flow = self._flow_factory(str(self._client_secrets), wanted)
            credentials = flow.run_local_server(
                port=0,
                open_browser=self._open_browser,
                # A closed browser tab must not leave the login waiting forever.
                timeout_seconds=OAUTH_CONSENT_TIMEOUT_S,
                authorization_prompt_message="",
                success_message="Авторизацію завершено. Можна закрити вкладку й повернутися до асистента.",
            )
        except Exception as exc:
            logger.warning("OAuth flow failed: %s", type(exc).__name__)
            raise OAuthError(
                "consent_denied",
                "Авторизацію Google скасовано або відхилено. Без дозволу календар і пошта недоступні.",
            ) from exc

        identity = self.fetch_identity(credentials)
        scopes = self._extract_granted_scopes(credentials)
        try:
            object.__setattr__(credentials, "granted_scopes", scopes)
        except Exception:
            pass
        try:
            self._store.save_record(identity.sub, credentials.to_json(), scopes)
        except TokenStoreError as exc:
            logger.error(
                "google.oauth.completed but token persist failed sub=%s… error=%s",
                identity.sub[:8],
                type(exc).__name__,
            )
            raise OAuthError(
                "token_store_failed",
                "Авторизацію Google завершено в браузері, але зберегти доступ на цьому комп'ютері "
                "не вдалося. Повтори підключення; без збережених credentials пошта недоступна.",
            ) from exc
        except Exception as exc:
            logger.error(
                "google.oauth.completed but token persist failed sub=%s… error=%s",
                identity.sub[:8],
                type(exc).__name__,
            )
            raise OAuthError(
                "token_store_failed",
                "Авторизацію Google завершено в браузері, але зберегти доступ на цьому комп'ютері "
                "не вдалося. Повтори підключення; без збережених credentials пошта недоступна.",
            ) from exc
        logger.info(
            "google.oauth.completed email=%s sub=%s… scopes=%s",
            identity.email,
            identity.sub[:8],
            sorted(scopes),
        )
        logger.info(
            "google.permission.granted sub=%s… scope_count=%s",
            identity.sub[:8],
            len(scopes),
        )
        return identity, credentials

    @staticmethod
    def _extract_granted_scopes(credentials: Credentials) -> list[str]:
        # Prefer token response granted scopes when the library exposes them.
        explicit = getattr(credentials, "granted_scopes", None)
        if explicit:
            return list(explicit)
        return list(credentials.scopes or [])

    def fetch_identity(self, credentials: Credentials) -> GoogleIdentity:
        service = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
        info = service.userinfo().get().execute()
        sub = (info.get("id") or "").strip()
        email = (info.get("email") or "").strip()
        name = (info.get("name") or email or "").strip()
        if not sub:
            raise OAuthError("identity_failed", "Google не повернув стабільний ідентифікатор користувача.")
        return GoogleIdentity(sub=sub, email=email, name=name)

    def load_credentials(self, google_sub: str) -> Credentials | None:
        """Restore credentials using persisted granted_scopes (never ALL_KNOWN)."""
        record = self._store.load_record(google_sub)
        if not record:
            with self._live_lock:
                self._live.pop(google_sub, None)
            return None
        creds_json, stored_scopes = record
        key = (creds_json, tuple(stored_scopes))
        with self._live_lock:
            cached = self._live.get(google_sub)
        if cached is not None and cached[0] == key:
            credentials = cached[1]
        else:
            credentials = self._credentials_from_record(creds_json, stored_scopes)
        credentials = self.refresh_if_needed(
            credentials,
            google_sub,
            stored_scopes=stored_scopes or list(credentials.scopes or []),
        )
        # A refresh re-saves the record: key the cache by what is stored now.
        current = self._store.load_record(google_sub)
        if current:
            with self._live_lock:
                self._live[google_sub] = ((current[0], tuple(current[1])), credentials)
        return credentials

    @staticmethod
    def _credentials_from_record(creds_json: str, stored_scopes: list[str]) -> Credentials:
        info = json.loads(creds_json)
        # Pass only the verified granted list — empty means trust whatever is in the JSON.
        if stored_scopes:
            credentials = Credentials.from_authorized_user_info(info, scopes=stored_scopes)
        else:
            credentials = Credentials.from_authorized_user_info(info)
        try:
            object.__setattr__(credentials, "granted_scopes", stored_scopes or list(credentials.scopes or []))
        except Exception:
            pass
        return credentials

    def refresh_if_needed(
        self,
        credentials: Credentials,
        google_sub: str,
        *,
        stored_scopes: list[str] | None = None,
    ) -> Credentials:
        scopes = stored_scopes if stored_scopes is not None else self._extract_granted_scopes(credentials)
        if credentials.valid:
            return credentials
        if not credentials.refresh_token:
            raise OAuthError(
                "reauth_required",
                "Немає refresh token — потрібна повторна авторизація Google.",
            )
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            logger.warning("Token refresh failed for sub=%s…: %s", google_sub[:8], type(exc).__name__)
            raise OAuthError(
                "revoked",
                "Доступ Google відкликано або прострочено. Підключіть акаунт знову.",
            ) from exc
        # Refresh must not invent new scopes — re-persist the known granted set.
        try:
            self._store.save_record(google_sub, credentials.to_json(), scopes)
        except TokenStoreError as exc:
            raise OAuthError(
                "token_store_failed",
                "Не вдалося зберегти оновлений доступ Google на цьому комп'ютері.",
            ) from exc
        return credentials

    def has_scopes(self, credentials: Credentials, required: tuple[str, ...] | list[str]) -> bool:
        explicit = getattr(credentials, "granted_scopes", None)
        granted = set(granted_scopes(credentials, explicit=list(explicit) if explicit else None))
        return all(scope in granted for scope in required)

    def missing_scopes(self, credentials: Credentials, required: tuple[str, ...] | list[str]) -> list[str]:
        explicit = getattr(credentials, "granted_scopes", None)
        granted = set(granted_scopes(credentials, explicit=list(explicit) if explicit else None))
        return [s for s in required if s not in granted]

    def require_scopes(self, google_sub: str, required: tuple[str, ...] | list[str]) -> Credentials:
        """Load credentials and verify scopes. Does NOT open the browser.

        Raises OAuthError(permission_required) if a scope was never granted —
        caller must invoke request_scopes() for incremental consent.
        """
        credentials = self.load_credentials(google_sub)
        if credentials is None:
            raise OAuthError("not_connected", "Google-акаунт ще не підключено.")
        missing = self.missing_scopes(credentials, required)
        if missing:
            labels = scope_labels(missing)
            raise OAuthError(
                "permission_required",
                "Немає дозволу: "
                + labels
                + ". Скажіть «дай доступ до "
                + labels
                + "» — одне вікно Google додасть усі відсутні дозволи (google_account grant_all).",
                missing_scopes=missing,
            )
        return credentials

    def request_scopes(
        self,
        google_sub: str,
        required: tuple[str, ...] | list[str],
    ) -> tuple[GoogleIdentity, Credentials]:
        """Incremental OAuth: keep existing grants, ask only for missing scopes (+ identity)."""
        existing = self.load_credentials(google_sub)
        current = list(granted_scopes(existing)) if existing else []
        wanted = list(dict.fromkeys(current + list(required) + list(IDENTITY_SCOPES)))
        identity, credentials = self.authorize(scopes=wanted)
        if identity.sub != google_sub:
            # New account chosen in browser — saved under new sub; signal mismatch.
            raise OAuthError(
                "account_mismatch",
                f"У браузері вибрано інший акаунт ({identity.email}). "
                "Щоб змінити активний акаунт, пройдіть «підключи Google» і оберіть потрібний у вікні входу.",
            )
        return identity, credentials

    # Back-compat name used by older call sites — check only, never silent full-scope inject.
    def ensure_scopes(self, google_sub: str, required: tuple[str, ...] | list[str]) -> Credentials:
        return self.require_scopes(google_sub, required)
