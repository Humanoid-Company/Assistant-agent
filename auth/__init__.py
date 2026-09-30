"""Google OAuth for the local desktop voice assistant."""

from auth.account_manager import AccountManager, AccountStatus
from auth.scopes import CALENDAR_SCOPES, GMAIL_SCOPES, IDENTITY_SCOPES

__all__ = [
    "AccountManager",
    "AccountStatus",
    "CALENDAR_SCOPES",
    "GMAIL_SCOPES",
    "IDENTITY_SCOPES",
]
