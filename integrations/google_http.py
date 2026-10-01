"""Shared transport for Google API clients.

- Discovery `build()` is cached per (api, version, account) — no re-parse per voice turn.
- HTTP connections are pooled per account and leased per request: httplib2 is not
  thread-safe, but a leased connection is used by one thread at a time, so TLS
  sessions to googleapis.com are reused across tool calls instead of a fresh
  handshake (~100–300 ms) on every call.
- Short socket timeout (a voice turn cannot wait the library default of 60 s).
- Read-only calls retry transient failures (429/5xx/network) with a short backoff.
  Mutations are never retried here — agents reconcile uncertain writes themselves.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from integrations.google_errors import map_google_error

logger = logging.getLogger(__name__)

GOOGLE_HTTP_TIMEOUT_S = float(os.getenv("GOOGLE_HTTP_TIMEOUT_S", "15"))
_READ_RETRY_DELAYS_S = (0.25, 0.7)
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_IDLE_PER_ACCOUNT = 4
_MAX_SERVICES = 32


def credentials_key(credentials: Any) -> tuple:
    """Stable identity for one account grant: a new consent / new scopes → a new key."""
    scopes = getattr(credentials, "granted_scopes", None) or getattr(credentials, "scopes", None) or ()
    token = getattr(credentials, "refresh_token", None) or id(credentials)
    return (token, tuple(sorted(scopes)))


def _new_http(credentials: Any) -> AuthorizedHttp:
    return AuthorizedHttp(credentials, http=httplib2.Http(timeout=GOOGLE_HTTP_TIMEOUT_S))


class _HttpPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[tuple, list[AuthorizedHttp]] = {}
        self._services: dict[tuple, Any] = {}

    def service(self, api: str, version: str, credentials: Any) -> Any:
        key = (api, version, credentials_key(credentials))
        with self._lock:
            cached = self._services.get(key)
        if cached is not None:
            return cached
        service = build(api, version, http=_new_http(credentials), cache_discovery=False)
        with self._lock:
            if len(self._services) >= _MAX_SERVICES:
                self._services.clear()
            return self._services.setdefault(key, service)

    @contextmanager
    def lease(self, credentials: Any) -> Iterator[AuthorizedHttp]:
        key = credentials_key(credentials)
        with self._lock:
            bucket = self._idle.get(key)
            http = bucket.pop() if bucket else None
        if http is None:
            http = _new_http(credentials)
        reusable = False
        try:
            yield http
            reusable = True
        except HttpError:
            reusable = True  # HTTP-level error: the connection itself is fine
            raise
        finally:
            if reusable:
                with self._lock:
                    bucket = self._idle.setdefault(key, [])
                    if len(bucket) < _MAX_IDLE_PER_ACCOUNT:
                        bucket.append(http)

    def clear(self) -> None:
        with self._lock:
            self._idle.clear()
            self._services.clear()


_POOL = _HttpPool()


def google_service(api: str, version: str, credentials: Any) -> Any:
    return _POOL.service(api, version, credentials)


def clear_google_transport() -> None:
    """Drop cached services and pooled connections (account switch / disconnect)."""
    _POOL.clear()


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, HttpError):
        return int(getattr(exc.resp, "status", 0) or 0) in _RETRYABLE_STATUSES
    return isinstance(exc, (OSError, TimeoutError, httplib2.HttpLib2Error))


def google_execute(request: Any, credentials: Any, *, read_only: bool) -> Any:
    """Execute an API request (or batch) on a pooled connection.

    Errors are mapped to GoogleApiError. Only `read_only` calls are retried.
    """
    attempts = 1 + (len(_READ_RETRY_DELAYS_S) if read_only else 0)
    for attempt in range(attempts):
        try:
            with _POOL.lease(credentials) as http:
                return request.execute(http=http)
        except Exception as exc:
            if attempt + 1 < attempts and _retryable(exc):
                delay = _READ_RETRY_DELAYS_S[attempt]
                logger.info("google.retry attempt=%s delay=%.2fs error=%s", attempt + 1, delay, type(exc).__name__)
                time.sleep(delay)
                continue
            raise map_google_error(exc) from exc
    raise AssertionError("unreachable")


def warm_up(credentials: Any, url: str) -> None:
    """Open (and pool) a TLS connection + refresh the access token ahead of the first real call."""
    with _POOL.lease(credentials) as http:
        http.request(url, "GET")
