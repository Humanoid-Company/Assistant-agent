"""Per-user Google credential storage with explicit granted_scopes metadata.

Production path: OS keyring. Never writes a shared plaintext token.json for all users.
Stored blob format (v2)::

    {"version": 2, "credentials": {…oauth json…}, "granted_scopes": ["…", …]}

Legacy plain Credentials.to_json() blobs are still readable (scopes from credentials only).
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "voice-agent-google-oauth"
_STORE_VERSION = 2


class TokenStore(ABC):
    @abstractmethod
    def save(self, google_sub: str, credentials_json: str) -> None: ...

    @abstractmethod
    def load(self, google_sub: str) -> str | None: ...

    @abstractmethod
    def delete(self, google_sub: str) -> None: ...

    @abstractmethod
    def list_subs(self) -> list[str]: ...

    def save_record(self, google_sub: str, credentials_json: str, granted_scopes: list[str]) -> None:
        envelope = {
            "version": _STORE_VERSION,
            "credentials": json.loads(credentials_json),
            "granted_scopes": list(granted_scopes),
        }
        self.save(google_sub, json.dumps(envelope))

    def load_record(self, google_sub: str) -> tuple[str, list[str]] | None:
        """Returns (credentials_json, granted_scopes) or None."""
        raw = self.load(google_sub)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and data.get("version") == _STORE_VERSION and "credentials" in data:
            scopes = [s for s in (data.get("granted_scopes") or []) if isinstance(s, str)]
            return json.dumps(data["credentials"]), scopes
        # Legacy: raw google credentials JSON
        scopes = []
        if isinstance(data, dict) and isinstance(data.get("scopes"), list):
            scopes = [s for s in data["scopes"] if isinstance(s, str)]
        return raw, scopes


class KeyringTokenStore(TokenStore):
    def __init__(self, service_name: str = _KEYRING_SERVICE) -> None:
        import keyring

        self._keyring = keyring
        self._service = service_name
        self._index_user = "__index__"

    def save(self, google_sub: str, credentials_json: str) -> None:
        if not google_sub:
            raise ValueError("google_sub is required")
        self._keyring.set_password(self._service, google_sub, credentials_json)
        index = set(self.list_subs())
        index.add(google_sub)
        self._keyring.set_password(self._service, self._index_user, json.dumps(sorted(index)))
        logger.info("Saved Google credentials for sub=%s…", google_sub[:8])

    def load(self, google_sub: str) -> str | None:
        return self._keyring.get_password(self._service, google_sub)

    def delete(self, google_sub: str) -> None:
        try:
            self._keyring.delete_password(self._service, google_sub)
        except Exception:
            pass
        index = [s for s in self.list_subs() if s != google_sub]
        self._keyring.set_password(self._service, self._index_user, json.dumps(index))
        logger.info("Deleted Google credentials for sub=%s…", google_sub[:8])

    def list_subs(self) -> list[str]:
        raw = self._keyring.get_password(self._service, self._index_user)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return list(data) if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


class InMemoryTokenStore(TokenStore):
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def save(self, google_sub: str, credentials_json: str) -> None:
        self._data[google_sub] = credentials_json

    def load(self, google_sub: str) -> str | None:
        return self._data.get(google_sub)

    def delete(self, google_sub: str) -> None:
        self._data.pop(google_sub, None)

    def list_subs(self) -> list[str]:
        return sorted(self._data)


def credentials_to_dict(credentials: Any) -> dict[str, Any]:
    return json.loads(credentials.to_json())
