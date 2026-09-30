"""Structured agent results spoken by the Realtime assistant."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AgentStatus = Literal[
    "success",
    "needs_more_info",
    "confirmation_required",
    "auth_required",
    "permission_required",
    "permission_denied",
    "not_found",
    "rate_limited",
    "ambiguous",
    "error",
]


@dataclass
class AgentResult:
    status: AgentStatus
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    awaiting_user_reply: bool = False

    def __post_init__(self) -> None:
        if self.status in (
            "needs_more_info",
            "confirmation_required",
            "auth_required",
            "permission_required",
        ):
            self.awaiting_user_reply = True


def result_from_google_error(exc: Exception) -> AgentResult:
    """Map GoogleApiError / OAuthError-like objects onto AgentResult without claiming success."""
    from auth.google_oauth import OAuthError
    from integrations.google_errors import GoogleApiError

    if isinstance(exc, OAuthError):
        if exc.code in ("not_connected", "reauth_required", "revoked"):
            return AgentResult("auth_required", str(exc), {"missing_scopes": exc.missing_scopes})
        if exc.code == "permission_required":
            return AgentResult("permission_required", str(exc), {"missing_scopes": exc.missing_scopes})
        if exc.code in ("consent_denied", "forbidden_switch"):
            return AgentResult("permission_denied", str(exc))
        return AgentResult("error", str(exc))

    if isinstance(exc, GoogleApiError):
        data = {"google_error": exc.code, "http_status": exc.http_status}
        if exc.http_status == 401 or exc.code in ("unauthorized", "auth_revoked"):
            return AgentResult("auth_required", str(exc), data)
        if exc.http_status == 403 or exc.code == "forbidden":
            return AgentResult("permission_denied", str(exc), data)
        if exc.http_status == 404 or exc.code == "not_found":
            return AgentResult("not_found", str(exc), data)
        if exc.http_status == 429 or exc.code == "rate_limited":
            return AgentResult("rate_limited", str(exc), data)
        return AgentResult("error", str(exc), data)

    return AgentResult("error", "Невідома помилка під час звернення до Google.")
