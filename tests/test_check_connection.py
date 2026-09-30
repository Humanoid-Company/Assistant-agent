"""Updated connection-status tests for local Google Agent Router (no n8n)."""
from __future__ import annotations

from agents.types import AgentResult
from assistant import _describe_connection_status, _parse_router_reply


def test_success_message_spoken_verbatim():
    msg = _describe_connection_status(AgentResult("success", "Підключено user@example.com. Календар: так. Пошта: ні."))
    assert "Підключено" in msg
    assert "n8n" not in msg.lower()


def test_auth_required_is_not_server_down():
    msg = _describe_connection_status(
        AgentResult("auth_required", "Google-акаунт ще не підключено — це не аварія сервера.")
    )
    assert "не аварія" in msg
    assert "agent-ecosystem" not in msg


def test_error_network_message():
    msg = _describe_connection_status(AgentResult("error", "Немає мережі або Google API недоступний."))
    assert "мережі" in msg or "Google" in msg


def test_parse_router_reply_confirmation_awaits_user():
    reply, awaiting = _parse_router_reply(
        AgentResult("confirmation_required", "Створити подію. Підтвердити?")
    )
    assert "Підтвердити" in reply
    assert awaiting is True


def test_parse_router_reply_success_does_not_await():
    reply, awaiting = _parse_router_reply(AgentResult("success", "Готово."))
    assert reply == "Готово."
    assert awaiting is False


def test_parse_router_reply_error_speaks_message():
    reply, awaiting = _parse_router_reply(AgentResult("error", "Google API тимчасово недоступний."))
    assert "недоступний" in reply
    assert awaiting is False
