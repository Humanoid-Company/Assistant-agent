"""Shared Google transport: pooled connections, read-only retries, batched Gmail search."""
from __future__ import annotations

import json

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.http import HttpMockSequence

import integrations.google_http as google_http
from integrations.google_errors import GoogleApiError
from integrations.google_gmail import GoogleGmailClient


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch):
    monkeypatch.setattr(google_http, "_POOL", google_http._HttpPool())
    monkeypatch.setattr(google_http, "_READ_RETRY_DELAYS_S", (0.0, 0.0))


def _creds(refresh_token: str = "refresh-a") -> Credentials:
    return Credentials(
        token="access",
        refresh_token=refresh_token,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


def _serve(monkeypatch, responses: list) -> list[HttpMockSequence]:
    created: list[HttpMockSequence] = []

    def new_http(_credentials):
        http = HttpMockSequence(list(responses))
        created.append(http)
        return http

    monkeypatch.setattr(google_http, "_new_http", new_http)
    return created


class _Request:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def execute(self, http=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _http_error(status: int):
    from googleapiclient.errors import HttpError

    return HttpError(type("Resp", (), {"status": status, "reason": "x"})(), b"{}")


def test_connection_is_reused_across_calls_for_the_same_account(monkeypatch):
    created = _serve(monkeypatch, [])
    creds = _creds()
    google_http.google_execute(_Request([{}]), creds, read_only=True)
    google_http.google_execute(_Request([{}]), creds, read_only=True)
    assert len(created) == 1


def test_different_accounts_never_share_a_connection(monkeypatch):
    created = _serve(monkeypatch, [])
    google_http.google_execute(_Request([{}]), _creds("refresh-a"), read_only=True)
    google_http.google_execute(_Request([{}]), _creds("refresh-b"), read_only=True)
    assert len(created) == 2


def test_reads_retry_transient_errors(monkeypatch):
    _serve(monkeypatch, [])
    request = _Request([_http_error(503), OSError("reset"), {"ok": True}])
    assert google_http.google_execute(request, _creds(), read_only=True) == {"ok": True}
    assert request.calls == 3


def test_writes_are_never_retried(monkeypatch):
    _serve(monkeypatch, [])
    request = _Request([_http_error(503), {"ok": True}])
    with pytest.raises(GoogleApiError) as info:
        google_http.google_execute(request, _creds(), read_only=False)
    assert info.value.code == "google_unavailable"
    assert request.calls == 1


def test_client_errors_are_not_retried(monkeypatch):
    _serve(monkeypatch, [])
    request = _Request([_http_error(404), {"ok": True}])
    with pytest.raises(GoogleApiError) as info:
        google_http.google_execute(request, _creds(), read_only=True)
    assert info.value.code == "not_found"
    assert request.calls == 1


def _batch_body(boundary: str, parts: dict[str, dict]) -> str:
    chunks = []
    for request_id, payload in parts.items():
        chunks.append(
            f"--{boundary}\r\n"
            "Content-Type: application/http\r\n"
            f"Content-ID: <response-base + {request_id}>\r\n\r\n"
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n\r\n"
            f"{json.dumps(payload)}\r\n"
        )
    return "".join(chunks) + f"--{boundary}--"


def _summary(message_id: str, subject: str) -> dict:
    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "snippet": "…",
        "payload": {"headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": "a@b.c"}]},
    }


def test_gmail_search_fetches_all_summaries_in_one_batch_round_trip(monkeypatch):
    import googleapiclient.http

    monkeypatch.setattr(googleapiclient.http.uuid, "uuid4", lambda: "base")
    boundary = "batch_boundary"
    listing = {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}
    batch = _batch_body(
        boundary,
        {"m1": _summary("m1", "Перший"), "m2": _summary("m2", "Другий"), "m3": _summary("m3", "Третій")},
    )
    created = _serve(
        monkeypatch,
        [
            ({"status": "200"}, json.dumps(listing)),
            ({"status": "200", "content-type": f"multipart/mixed; boundary={boundary}"}, batch),
        ],
    )

    results = GoogleGmailClient(_creds()).search("from:a@b.c", max_results=3)

    assert [r.get("subject") for r in results] == ["Перший", "Другий", "Третій"]
    # Exactly two round trips (list + ONE batch), both on the same pooled connection;
    # created[0] is the service's default http, which pooled execution never touches.
    service_default, pooled = created
    assert pooled._iterable == []
    assert len(service_default._iterable) == 2
