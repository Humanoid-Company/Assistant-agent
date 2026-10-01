"""Regression: works without n8n / agent-ecosystem; secrets stay out of logs/git patterns."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from agents.types import AgentResult
from assistant import TOOLS, _parse_router_reply
from tests.helpers_google import build_test_router


def test_router_works_with_no_n8n_env(tmp_path, monkeypatch):
    monkeypatch.delenv("N8N_ROUTER_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AGENT_ECOSYSTEM_HEALTH_URL", raising=False)
    router, *_ = build_test_router(tmp_path)
    r = router.check_connection()
    assert r.status in ("success", "auth_required")
    assert "n8n" not in r.message.lower()


def test_tools_include_google_typed_actions_not_n8n_only():
    names = {t["name"] for t in TOOLS}
    assert "calendar_action" in names
    assert "gmail_action" in names
    assert "google_account" in names
    assert "dispatch_task" in names


def test_assistant_has_no_robot_tools():
    from tools.live_schemas import LIVE_BACKEND_TOOLS

    assert "control_robot" not in {t["name"] for t in TOOLS}
    assert "control_robot" not in {t["name"] for t in LIVE_BACKEND_TOOLS}


def test_gitignore_covers_secrets():
    text = Path(".gitignore").read_text(encoding="utf-8")
    assert "client_secret" in text
    assert ".env" in text
    assert "credentials" in text


def test_logs_do_not_contain_refresh_tokens(tmp_path, caplog):
    router, *_ = build_test_router(tmp_path)
    with caplog.at_level(logging.INFO):
        router.google_status()
        router.calendar_action(action="list")
    blob = " ".join(r.message for r in caplog.records)
    assert "1//fake-refresh" not in blob
    assert "ya29.fake-access" not in blob
    assert "fake-secret" not in blob
    assert not re.search(r"refresh_token", blob, re.I)


def test_parse_never_claims_success_on_error_status():
    reply, awaiting = _parse_router_reply(AgentResult("error", "Помилка Google API (500)."))
    assert "500" in reply or "Помилка" in reply
    assert awaiting is False
