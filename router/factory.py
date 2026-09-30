"""
Factory wiring for local Google agents — used by assistant.py and tests.
"""
from __future__ import annotations

import logging
from pathlib import Path

from agents.calendar_agent import CalendarAgent
from agents.gmail_agent import GmailAgent
from agents.pending_store import PendingStore
from auth.account_manager import AccountManager
from auth.google_oauth import GoogleOAuthClient
from auth.token_store import InMemoryTokenStore, KeyringTokenStore, TokenStore
from router.agent_router import AgentRouter

logger = logging.getLogger(__name__)


def _make_token_store(use_keyring: bool) -> TokenStore:
    if not use_keyring:
        return InMemoryTokenStore()
    try:
        store = KeyringTokenStore()
        # Probe backend early — some CI/headless environments have no keyring.
        store.list_subs()
        return store
    except Exception as exc:
        logger.warning("OS keyring unavailable (%s) — using in-memory token store for this process.", type(exc).__name__)
        return InMemoryTokenStore()


def build_agent_router(
    *,
    client_secrets_file: Path | str,
    state_file: Path | str,
    timezone: str = "Europe/Kyiv",
    token_store: TokenStore | None = None,
    use_keyring: bool = True,
    shared_device: bool | None = None,
    session_idle_timeout_s: float | None = None,
) -> AgentRouter:
    import os

    if shared_device is None:
        shared_device = os.getenv("SHARED_DEVICE_MODE", "false").lower() in ("1", "true", "yes")
    if session_idle_timeout_s is None:
        session_idle_timeout_s = float(os.getenv("SESSION_IDLE_TIMEOUT_S", "300"))

    store = token_store or _make_token_store(use_keyring)
    oauth = GoogleOAuthClient(client_secrets_file, store)
    accounts = AccountManager(
        oauth,
        store,
        Path(state_file),
        shared_device=shared_device,
        session_idle_timeout_s=session_idle_timeout_s,
    )
    pending = PendingStore()
    calendar = CalendarAgent(accounts, pending, timezone=timezone)
    gmail = GmailAgent(accounts, pending)
    return AgentRouter(accounts, calendar, gmail, pending)
