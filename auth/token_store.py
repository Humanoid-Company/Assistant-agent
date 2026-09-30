"""Per-user Google credential storage with explicit granted_scopes metadata.

Production path: OS keyring. Never writes a shared plaintext token.json for all users.

Stored blob formats
-------------------
Legacy v2 (single keyring value per sub)::

    {"version": 2, "credentials": {…}, "granted_scopes": ["…"]}

Chunked v3 (Windows Credential Manager size-safe)::

    key ``sub`` → manifest {"version": 3, "storage": "chunked", "chunks": N}
    key ``sub::chunk::i`` → UTF-8-safe payload fragment

Legacy plain Credentials.to_json() blobs are still readable.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "voice-agent-google-oauth"
_STORE_VERSION = 2
_CHUNKED_VERSION = 3
# Conservative UTF-8 byte budget — well under Windows CredWrite generic-blob limits.
_CHUNK_MAX_BYTES = 1200
_CHUNK_KEY_FMT = "{sub}::chunk::{index}"
_MAX_CHUNKS = 64


class TokenStoreError(Exception):
    """Credential persistence failed — OAuth must not report success."""


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
        self.save(google_sub, json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))

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


def _chunk_utf8(text: str, max_bytes: int) -> list[str]:
    """Split ``text`` into chunks each encoding to at most ``max_bytes`` UTF-8 bytes."""
    if max_bytes < 4:
        raise ValueError("max_bytes must allow at least one Unicode code point")
    chunks: list[str] = []
    buf = ""
    for ch in text:
        candidate = buf + ch
        if len(candidate.encode("utf-8")) > max_bytes and buf:
            chunks.append(buf)
            buf = ch
            if len(buf.encode("utf-8")) > max_bytes:
                raise TokenStoreError("Single character exceeds chunk size budget.")
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks or [""]


def _is_chunk_manifest(data: object) -> bool:
    return (
        isinstance(data, dict)
        and data.get("version") == _CHUNKED_VERSION
        and data.get("storage") == "chunked"
        and isinstance(data.get("chunks"), int)
    )


class KeyringTokenStore(TokenStore):
    def __init__(
        self,
        service_name: str = _KEYRING_SERVICE,
        *,
        chunk_max_bytes: int = _CHUNK_MAX_BYTES,
        keyring_backend: Any | None = None,
    ) -> None:
        if keyring_backend is None:
            import keyring

            keyring_backend = keyring
        self._keyring = keyring_backend
        self._service = service_name
        self._index_user = "__index__"
        self._chunk_max_bytes = chunk_max_bytes

    def _chunk_key(self, google_sub: str, index: int) -> str:
        return _CHUNK_KEY_FMT.format(sub=google_sub, index=index)

    def _backend_name(self) -> str:
        backend = getattr(self._keyring, "get_keyring", None)
        if callable(backend):
            try:
                return type(backend()).__name__
            except Exception:
                pass
        return type(self._keyring).__name__

    def save(self, google_sub: str, credentials_json: str) -> None:
        if not google_sub:
            raise ValueError("google_sub is required")
        payload_bytes = len(credentials_json.encode("utf-8"))
        logger.info(
            "google.token_store save sub=%s payload_bytes=%s backend=%s",
            google_sub[:8],
            payload_bytes,
            self._backend_name(),
        )
        try:
            self._save_chunked(google_sub, credentials_json)
        except TokenStoreError:
            logger.error(
                "google.token_store.save_failed sub=%s payload_bytes=%s",
                google_sub[:8],
                payload_bytes,
            )
            raise
        except Exception as exc:
            logger.error(
                "google.token_store.save_failed sub=%s payload_bytes=%s error=%s",
                google_sub[:8],
                payload_bytes,
                type(exc).__name__,
            )
            raise TokenStoreError(
                f"Failed to persist Google credentials ({type(exc).__name__})."
            ) from exc
        logger.info(
            "google.token_store.saved sub=%s payload_bytes=%s",
            google_sub[:8],
            payload_bytes,
        )

    def _save_chunked(self, google_sub: str, credentials_json: str) -> None:
        chunks = _chunk_utf8(credentials_json, self._chunk_max_bytes)
        if len(chunks) > _MAX_CHUNKS:
            raise TokenStoreError("Credential payload requires too many chunks.")

        written: list[int] = []
        manifest_written = False
        try:
            for index, chunk in enumerate(chunks):
                self._keyring.set_password(self._service, self._chunk_key(google_sub, index), chunk)
                written.append(index)

            reassembled = self._read_chunks(google_sub, len(chunks))
            if reassembled != credentials_json:
                raise TokenStoreError("Chunk reassembly verification failed.")

            manifest = json.dumps(
                {"version": _CHUNKED_VERSION, "storage": "chunked", "chunks": len(chunks)},
                separators=(",", ":"),
            )
            self._keyring.set_password(self._service, google_sub, manifest)
            manifest_written = True

            index = set(self.list_subs())
            index.add(google_sub)
            self._keyring.set_password(self._service, self._index_user, json.dumps(sorted(index)))

            # Drop leftover chunks from a previous larger save.
            for stale in range(len(chunks), _MAX_CHUNKS):
                self._delete_password(self._chunk_key(google_sub, stale))
        except Exception:
            # Only scrub partial chunks when the main key was not yet switched to the
            # chunked manifest — otherwise leave a consistent readable record.
            if not manifest_written:
                for index in written:
                    self._delete_password(self._chunk_key(google_sub, index))
            raise

    def _read_chunks(self, google_sub: str, count: int) -> str:
        parts: list[str] = []
        for index in range(count):
            part = self._keyring.get_password(self._service, self._chunk_key(google_sub, index))
            if part is None:
                raise TokenStoreError(f"Missing credential chunk {index}.")
            parts.append(part)
        return "".join(parts)

    def load(self, google_sub: str) -> str | None:
        raw = self._keyring.get_password(self._service, google_sub)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if _is_chunk_manifest(data):
            count = int(data["chunks"])
            if count < 0 or count > _MAX_CHUNKS:
                logger.error("google.token_store corrupt manifest sub=%s…", google_sub[:8])
                return None
            try:
                return self._read_chunks(google_sub, count)
            except TokenStoreError:
                logger.error("google.token_store incomplete chunks sub=%s…", google_sub[:8])
                return None
        # Legacy single-value JSON (v2 envelope or raw credentials).
        return raw

    def delete(self, google_sub: str) -> None:
        # Discover chunk count from manifest when present.
        raw = self._keyring.get_password(self._service, google_sub)
        chunk_count = 0
        if raw:
            try:
                data = json.loads(raw)
                if _is_chunk_manifest(data):
                    chunk_count = int(data.get("chunks") or 0)
            except json.JSONDecodeError:
                pass
        self._delete_password(google_sub)
        for index in range(max(chunk_count, _MAX_CHUNKS)):
            self._delete_password(self._chunk_key(google_sub, index))
        index = [s for s in self.list_subs() if s != google_sub]
        try:
            self._keyring.set_password(self._service, self._index_user, json.dumps(index))
        except Exception:
            pass
        logger.info("Deleted Google credentials for sub=%s…", google_sub[:8])

    def _delete_password(self, username: str) -> None:
        try:
            self._keyring.delete_password(self._service, username)
        except Exception:
            pass

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
