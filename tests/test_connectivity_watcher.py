from __future__ import annotations

from assistant import _ConnectivityWatcher


def test_no_alert_while_staying_healthy():
    watcher = _ConnectivityWatcher()
    watcher.record_check_result(True, "problem A")
    watcher.record_check_result(True, "problem A")
    assert watcher.pop_alert() is None


def test_healthy_to_broken_arms_an_alert():
    watcher = _ConnectivityWatcher()
    watcher.record_check_result(True, "n/a")
    watcher.record_check_result(False, "n8n не проксіює")
    assert watcher.pop_alert() == "n8n не проксіює"


def test_staying_broken_does_not_rearm_after_pop():
    """Otherwise every single wake would repeat the same alert until someone fixes it."""
    watcher = _ConnectivityWatcher()
    watcher.record_check_result(True, "n/a")
    watcher.record_check_result(False, "n8n не проксіює")
    assert watcher.pop_alert() == "n8n не проксіює"
    watcher.record_check_result(False, "n8n не проксіює")
    assert watcher.pop_alert() is None


def test_recovering_resets_silently():
    watcher = _ConnectivityWatcher()
    watcher.record_check_result(True, "n/a")
    watcher.record_check_result(False, "n8n не проксіює")
    watcher.record_check_result(True, "n/a")
    assert watcher.pop_alert() is None


def test_can_alert_again_on_a_second_unrelated_failure():
    watcher = _ConnectivityWatcher()
    watcher.record_check_result(True, "n/a")
    watcher.record_check_result(False, "n8n не проксіює")
    watcher.pop_alert()
    watcher.record_check_result(True, "n/a")
    watcher.record_check_result(False, "agent-ecosystem не запущений")
    assert watcher.pop_alert() == "agent-ecosystem не запущений"


def test_pop_clears_pending_alert():
    watcher = _ConnectivityWatcher()
    watcher.record_check_result(True, "n/a")
    watcher.record_check_result(False, "проблема")
    assert watcher.pop_alert() == "проблема"
    assert watcher.pop_alert() is None
