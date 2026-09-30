"""KeyringTokenStore chunking — simulates Windows CredWrite size limits."""
from __future__ import annotations

import json

import pytest

from auth.token_store import KeyringTokenStore, TokenStoreError, _chunk_utf8


class SizeLimitedFakeKeyring:
    """In-memory keyring that rejects oversized values (byte-accurate)."""

    def __init__(self, max_bytes: int = 1500) -> None:
        self.max_bytes = max_bytes
        self._data: dict[tuple[str, str], str] = {}
        self.fail_next_set: bool = False
        self.set_calls = 0

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_calls += 1
        if self.fail_next_set:
            self.fail_next_set = False
            raise OSError(1783, "The stub received bad data")
        raw = password.encode("utf-8")
        if len(raw) > self.max_bytes:
            raise OSError(1783, "The stub received bad data")
        self._data[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self._data.pop((service, username), None)


def _large_credentials_json(*, kilobytes: int = 8) -> str:
    # Simulate a v2 envelope enlarged by Gmail scopes + long token fields.
    scopes = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    ]
    padding = "п" * (kilobytes * 200)  # multi-byte Cyrillic to stress UTF-8 chunking
    envelope = {
        "version": 2,
        "credentials": {
            "token": "ya29." + ("A" * 800),
            "refresh_token": "1//" + ("R" * 400),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake.apps.googleusercontent.com",
            "client_secret": "fake-secret",
            "scopes": scopes,
            "note": padding,
        },
        "granted_scopes": scopes,
    }
    return json.dumps(envelope, ensure_ascii=False)


def test_chunk_utf8_respects_byte_budget():
    text = "café" + ("я" * 500)
    chunks = _chunk_utf8(text, max_bytes=40)
    assert "".join(chunks) == text
    assert all(len(c.encode("utf-8")) <= 40 for c in chunks)


def test_large_payload_save_load_byte_identical():
    backend = SizeLimitedFakeKeyring(max_bytes=1500)
    store = KeyringTokenStore(service_name="test-oauth", chunk_max_bytes=1000, keyring_backend=backend)
    payload = _large_credentials_json(kilobytes=8)
    assert len(payload.encode("utf-8")) > 1500
    store.save("sub-alice", payload)
    loaded = store.load("sub-alice")
    assert loaded == payload
    assert "sub-alice" in store.list_subs()
    # Manifest is small; chunks exist.
    manifest = json.loads(backend.get_password("test-oauth", "sub-alice"))
    assert manifest["storage"] == "chunked"
    assert manifest["chunks"] >= 2


def test_legacy_single_record_still_loads():
    backend = SizeLimitedFakeKeyring(max_bytes=8000)
    store = KeyringTokenStore(service_name="test-oauth", chunk_max_bytes=1000, keyring_backend=backend)
    legacy = json.dumps(
        {
            "version": 2,
            "credentials": {"token": "ya29.small", "refresh_token": "1//x"},
            "granted_scopes": ["openid"],
        }
    )
    # Simulate pre-migration single-blob write.
    backend.set_password("test-oauth", "sub-legacy", legacy)
    backend.set_password("test-oauth", "__index__", json.dumps(["sub-legacy"]))
    assert store.load("sub-legacy") == legacy
    record = store.load_record("sub-legacy")
    assert record is not None
    creds_json, scopes = record
    assert "ya29.small" in creds_json
    assert "openid" in scopes


def test_delete_removes_all_chunks_and_index():
    backend = SizeLimitedFakeKeyring(max_bytes=1500)
    store = KeyringTokenStore(service_name="test-oauth", chunk_max_bytes=800, keyring_backend=backend)
    payload = _large_credentials_json(kilobytes=6)
    store.save("sub-del", payload)
    store.delete("sub-del")
    assert store.load("sub-del") is None
    assert "sub-del" not in store.list_subs()
    leftovers = [k for k in backend._data if k[1].startswith("sub-del")]
    assert leftovers == []


def test_partial_write_failure_does_not_produce_valid_load():
    backend = SizeLimitedFakeKeyring(max_bytes=1500)
    store = KeyringTokenStore(service_name="test-oauth", chunk_max_bytes=900, keyring_backend=backend)
    # Seed a good legacy record that must survive a failed upgrade attempt.
    legacy = json.dumps({"version": 2, "credentials": {"token": "keep-me"}, "granted_scopes": []})
    backend.set_password("test-oauth", "sub-keep", legacy)
    backend.set_password("test-oauth", "__index__", json.dumps(["sub-keep"]))

    large = _large_credentials_json(kilobytes=5)
    # Fail while writing the second password (first chunk already written).
    original_set = backend.set_password
    calls = {"n": 0}

    def flaky(service, username, password):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(1783, "The stub received bad data")
        return original_set(service, username, password)

    backend.set_password = flaky  # type: ignore[method-assign]
    with pytest.raises(TokenStoreError):
        store.save("sub-keep", large)
    # Main key still legacy; load still works.
    assert store.load("sub-keep") == legacy


def test_unicode_scopes_reassemble():
    backend = SizeLimitedFakeKeyring(max_bytes=1200)
    store = KeyringTokenStore(service_name="test-oauth", chunk_max_bytes=500, keyring_backend=backend)
    payload = json.dumps(
        {
            "version": 2,
            "credentials": {"token": "t", "note": "Українська пошта " + ("ї" * 800)},
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.send"],
        },
        ensure_ascii=False,
    )
    store.save("sub-ua", payload)
    assert store.load("sub-ua") == payload


def test_index_remains_correct_across_users():
    backend = SizeLimitedFakeKeyring(max_bytes=2000)
    store = KeyringTokenStore(service_name="test-oauth", chunk_max_bytes=700, keyring_backend=backend)
    a = _large_credentials_json(kilobytes=4)
    b = _large_credentials_json(kilobytes=4)
    store.save("sub-a", a)
    store.save("sub-b", b)
    assert store.list_subs() == ["sub-a", "sub-b"]
    store.delete("sub-a")
    assert store.list_subs() == ["sub-b"]
    assert store.load("sub-b") == b
