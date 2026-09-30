"""Tests for _parse_router_reply against AgentResult (local router, no HTTP envelope)."""
from __future__ import annotations

from agents.types import AgentResult
from assistant import _parse_router_reply


def test_success_speaks_the_message():
    reply, awaiting = _parse_router_reply(AgentResult("success", "Готово, зустріч створено."))
    assert reply == "Готово, зустріч створено."
    assert awaiting is False


def test_confirmation_required_flags_awaiting_user_reply():
    reply, awaiting = _parse_router_reply(
        AgentResult("confirmation_required", "Зустріч о 15:00. Підтвердити?")
    )
    assert reply == "Зустріч о 15:00. Підтвердити?"
    assert awaiting is True


def test_needs_more_info_flags_awaiting_user_reply():
    reply, awaiting = _parse_router_reply(AgentResult("needs_more_info", "На яку дату?"))
    assert reply == "На яку дату?"
    assert awaiting is True


def test_auth_required_awaits_and_speaks():
    reply, awaiting = _parse_router_reply(AgentResult("auth_required", "Підключіть Google."))
    assert "Google" in reply
    assert awaiting is True


def test_error_speaks_specific_message():
    reply, awaiting = _parse_router_reply(
        AgentResult("error", "Виникла технічна помилка. Спробуйте пізніше.")
    )
    assert reply == "Виникла технічна помилка. Спробуйте пізніше."
    assert awaiting is False


def test_empty_message_falls_back():
    reply, awaiting = _parse_router_reply(AgentResult("success", "   "))
    assert reply == "Роутер нічого не відповів."
    assert awaiting is False
