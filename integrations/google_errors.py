"""Shared Google API error mapping — no secrets in messages."""
from __future__ import annotations

from dataclasses import dataclass

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError


@dataclass
class GoogleApiError(Exception):
    code: str
    http_status: int | None
    message: str

    def __str__(self) -> str:
        return self.message


def map_google_error(exc: BaseException) -> GoogleApiError:
    if isinstance(exc, RefreshError):
        return GoogleApiError(
            code="auth_revoked",
            http_status=401,
            message="Доступ Google відкликано або прострочено. Підключіть акаунт знову.",
        )
    if isinstance(exc, HttpError):
        status = int(getattr(exc.resp, "status", 0) or 0)
        if status == 401:
            return GoogleApiError("unauthorized", 401, "Google відхилив доступ (401). Потрібна повторна авторизація.")
        if status == 403:
            detail = ""
            try:
                detail = (exc.content or b"").decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            lower = detail.lower()
            if (
                "accessnotconfigured" in lower
                or "service_disabled" in lower
                or "has not been used" in lower
                or "is disabled" in lower
            ):
                api_hint = "Google API"
                if "drive" in lower:
                    api_hint = "Google Drive API"
                elif "docs" in lower or "document" in lower:
                    api_hint = "Google Docs API"
                return GoogleApiError(
                    "api_disabled",
                    403,
                    f"{api_hint} не увімкнено в Google Cloud Console для цього проєкту.",
                )
            return GoogleApiError(
                "forbidden",
                403,
                "Немає дозволу на цю дію в Google (403). Перевірте scopes або політику акаунта.",
            )
        if status == 404:
            return GoogleApiError("not_found", 404, "Ресурс не знайдено в Google (404).")
        if status == 409:
            return GoogleApiError("conflict", 409, "Конфлікт даних у Google (409).")
        if status == 429:
            return GoogleApiError("rate_limited", 429, "Занадто багато запитів до Google (429). Спробуйте трохи пізніше.")
        if status >= 500:
            return GoogleApiError("google_unavailable", status, "Google API тимчасово недоступний. Спробуйте пізніше.")
        return GoogleApiError("google_error", status, f"Помилка Google API ({status}).")
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return GoogleApiError("timeout", None, "Час очікування відповіді Google вичерпано.")
    if isinstance(exc, OSError) or "connection" in str(exc).lower() or "network" in type(exc).__name__.lower():
        return GoogleApiError("network", None, "Немає мережі або з'єднання з Google обірвалось.")
    return GoogleApiError("unknown", None, "Невідома помилка під час звернення до Google.")
