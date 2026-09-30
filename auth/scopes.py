"""Minimal OAuth scopes — identity first; Calendar/Gmail granted independently."""

from __future__ import annotations

IDENTITY_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

CALENDAR_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar",
)

GMAIL_READONLY_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
)

GMAIL_COMPOSE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.compose",
)

GMAIL_SEND_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
)

# Full Gmail set (search/read + draft + send). Requested together on first Gmail use.
GMAIL_SCOPES: tuple[str, ...] = GMAIL_READONLY_SCOPES + GMAIL_COMPOSE_SCOPES + GMAIL_SEND_SCOPES

ALL_KNOWN_SCOPES: tuple[str, ...] = IDENTITY_SCOPES + CALENDAR_SCOPES + GMAIL_SCOPES


def scope_labels(missing: list[str] | tuple[str, ...]) -> str:
    """Human-readable Ukrainian labels for missing scopes (no raw URLs in voice)."""
    labels: list[str] = []
    missing_set = set(missing)
    if any(s in missing_set for s in CALENDAR_SCOPES):
        labels.append("Google Календар")
    if any(s in missing_set for s in GMAIL_READONLY_SCOPES):
        labels.append("читання Gmail")
    if any(s in missing_set for s in GMAIL_COMPOSE_SCOPES):
        labels.append("чернетки Gmail")
    if any(s in missing_set for s in GMAIL_SEND_SCOPES):
        labels.append("надсилання Gmail")
    return ", ".join(labels) if labels else "додаткові дозволи Google"
