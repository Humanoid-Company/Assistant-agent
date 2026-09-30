"""Shared fakes for Google migration tests — no real credentials or network."""
from __future__ import annotations

import json
from pathlib import Path

from agents.calendar_agent import CalendarAgent
from agents.gmail_agent import GmailAgent
from agents.pending_store import PendingStore
from auth.account_manager import AccountManager
from auth.google_oauth import GoogleIdentity, GoogleOAuthClient, OAuthError, granted_scopes
from auth.scopes import CALENDAR_SCOPES, GMAIL_SCOPES, IDENTITY_SCOPES
from auth.token_store import InMemoryTokenStore
from google.oauth2.credentials import Credentials
from integrations.google_calendar import FakeCalendarClient
from integrations.google_gmail import FakeGmailClient
from router.agent_router import AgentRouter


def _fake_creds(*, scopes: list[str] | None = None) -> Credentials:
    return Credentials(
        token="ya29.fake-access",
        refresh_token="1//fake-refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="fake-client.apps.googleusercontent.com",
        client_secret="fake-secret",
        scopes=scopes or list(IDENTITY_SCOPES + CALENDAR_SCOPES),
    )


class FakeOAuth(GoogleOAuthClient):
    def __init__(self, store: InMemoryTokenStore, identities: dict[str, GoogleIdentity] | None = None):
        self._store = store
        self._identities = identities or {}
        self._client_secrets = Path("credentials/client_secret.json")
        self.authorize_calls = 0
        self.deny_next = False
        self.revoked_subs: set[str] = set()
        self.open_browser = False
        self._next_authorize_scopes: list[str] | None = None
        self._next_identity: GoogleIdentity | None = None

    def ensure_client_secrets(self) -> None:
        return None

    def authorize(self, scopes=None):
        self.authorize_calls += 1
        if self.deny_next:
            self.deny_next = False
            raise OAuthError("consent_denied", "Авторизацію Google скасовано або відхилено.")
        if self._next_identity is not None:
            identity = self._next_identity
            self._next_identity = None
        elif self._identities:
            identity = next(iter(self._identities.values()))
        else:
            identity = GoogleIdentity(sub="sub-default", email="user@example.com", name="User")
        granted = list(self._next_authorize_scopes or scopes or list(IDENTITY_SCOPES + CALENDAR_SCOPES))
        self._next_authorize_scopes = None
        creds = _fake_creds(scopes=granted)
        try:
            object.__setattr__(creds, "granted_scopes", granted)
        except Exception:
            pass
        self._store.save_record(identity.sub, creds.to_json(), granted)
        self._identities[identity.sub] = identity
        return identity, creds

    def fetch_identity(self, credentials):
        return next(iter(self._identities.values()))

    def load_credentials(self, google_sub: str):
        if google_sub in self.revoked_subs:
            raise OAuthError("revoked", "Доступ Google відкликано або прострочено.")
        record = self._store.load_record(google_sub)
        if not record:
            return None
        creds_json, stored_scopes = record
        info = json.loads(creds_json)
        if stored_scopes:
            creds = Credentials.from_authorized_user_info(info, scopes=stored_scopes)
        else:
            creds = Credentials.from_authorized_user_info(info)
        try:
            object.__setattr__(creds, "granted_scopes", stored_scopes or list(creds.scopes or []))
        except Exception:
            pass
        return creds

    def refresh_if_needed(self, credentials, google_sub: str, *, stored_scopes=None):
        if google_sub in self.revoked_subs:
            raise OAuthError("revoked", "Доступ Google відкликано або прострочено.")
        return credentials

    def has_scopes(self, credentials, required):
        explicit = getattr(credentials, "granted_scopes", None)
        granted = set(granted_scopes(credentials, explicit=list(explicit) if explicit else None))
        return all(s in granted for s in required)

    def missing_scopes(self, credentials, required):
        explicit = getattr(credentials, "granted_scopes", None)
        granted = set(granted_scopes(credentials, explicit=list(explicit) if explicit else None))
        return [s for s in required if s not in granted]

    def require_scopes(self, google_sub: str, required):
        creds = self.load_credentials(google_sub)
        if creds is None:
            raise OAuthError("not_connected", "Google-акаунт ще не підключено.")
        missing = self.missing_scopes(creds, required)
        if missing:
            raise OAuthError(
                "permission_required",
                "Немає дозволу на цю дію. Потрібен додатковий consent.",
                missing_scopes=missing,
            )
        return creds

    def request_scopes(self, google_sub: str, required):
        existing = self.load_credentials(google_sub)
        if existing:
            explicit = getattr(existing, "granted_scopes", None)
            current = list(explicit) if explicit else list(granted_scopes(existing))
        else:
            current = []
        wanted = list(dict.fromkeys(current + list(required) + list(IDENTITY_SCOPES)))
        self._next_authorize_scopes = wanted
        if google_sub in self._identities:
            identity = self._identities[google_sub]
            self._identities = {google_sub: identity, **{k: v for k, v in self._identities.items() if k != google_sub}}
            self._next_identity = identity
        return self.authorize(scopes=wanted)

    def ensure_scopes(self, google_sub: str, required):
        return self.require_scopes(google_sub, required)


def build_test_router(
    tmp_path: Path,
    *,
    calendar: FakeCalendarClient | None = None,
    gmail: FakeGmailClient | None = None,
    seed_user: GoogleIdentity | None = None,
    scopes: list[str] | None = None,
    shared_device: bool = False,
    session_idle_timeout_s: float = 300.0,
) -> tuple[AgentRouter, FakeCalendarClient, FakeGmailClient, FakeOAuth, AccountManager]:
    store = InMemoryTokenStore()
    identity = seed_user or GoogleIdentity(sub="sub-alice", email="alice@example.com", name="Alice")
    oauth = FakeOAuth(store, {identity.sub: identity})
    accounts = AccountManager(
        oauth,
        store,
        tmp_path / "accounts.json",
        shared_device=shared_device,
        session_idle_timeout_s=session_idle_timeout_s,
    )
    granted = list(scopes or FULL_SCOPES)
    store.save_record(identity.sub, _fake_creds(scopes=granted).to_json(), granted)
    accounts._profiles[identity.sub] = {"email": identity.email, "name": identity.name}
    accounts._active_sub = identity.sub
    accounts._session_touch()
    accounts._save_state()

    cal = calendar or FakeCalendarClient()
    mail = gmail or FakeGmailClient()
    pending = PendingStore(ttl_seconds=300)
    cal_agent = CalendarAgent(accounts, pending, timezone="Europe/Kyiv", client_factory=lambda _c: cal)
    mail_agent = GmailAgent(accounts, pending, client_factory=lambda _c: mail)
    router = AgentRouter(accounts, cal_agent, mail_agent, pending)
    return router, cal, mail, oauth, accounts


CALENDAR_ONLY_SCOPES = list(IDENTITY_SCOPES + CALENDAR_SCOPES)
FULL_SCOPES = list(IDENTITY_SCOPES + CALENDAR_SCOPES + GMAIL_SCOPES)
