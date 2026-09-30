"""Tests for instance_lock.py — the PID-file guard that stops a second `python main.py` from
running at once (both instances would listen to the mic and speak simultaneously, the literal
"two assistants talking over each other" incident this project already had).
"""
from __future__ import annotations

import os

import pytest

from instance_lock import AlreadyRunningError, SingleInstanceLock, _pid_is_alive


def test_pid_is_alive_for_current_process():
    assert _pid_is_alive(os.getpid()) is True


def test_pid_is_alive_false_for_a_pid_that_does_not_exist():
    # PIDs this high don't exist on any real system.
    assert _pid_is_alive(2**30) is False


def test_acquire_creates_pid_file_with_our_own_pid(tmp_path):
    lock = SingleInstanceLock(tmp_path / "assistant.pid")
    lock.acquire()
    assert int((tmp_path / "assistant.pid").read_text().strip()) == os.getpid()


def test_acquire_raises_when_another_live_process_holds_it(tmp_path):
    path = tmp_path / "assistant.pid"
    path.write_text(str(os.getpid()))  # our own PID stands in for "a live process"
    lock = SingleInstanceLock(path)
    with pytest.raises(AlreadyRunningError) as exc_info:
        lock.acquire()
    assert exc_info.value.pid == os.getpid()


def test_acquire_reclaims_a_stale_lock_from_a_dead_pid(tmp_path):
    path = tmp_path / "assistant.pid"
    path.write_text(str(2**30))  # nobody's using this PID
    lock = SingleInstanceLock(path)
    lock.acquire()  # must not raise
    assert int(path.read_text().strip()) == os.getpid()


def test_acquire_reclaims_a_corrupt_lock_file(tmp_path):
    path = tmp_path / "assistant.pid"
    path.write_text("not-a-pid")
    lock = SingleInstanceLock(path)
    lock.acquire()  # must not raise
    assert int(path.read_text().strip()) == os.getpid()


def test_release_removes_the_file(tmp_path):
    path = tmp_path / "assistant.pid"
    lock = SingleInstanceLock(path)
    lock.acquire()
    lock.release()
    assert not path.exists()


def test_release_without_acquire_is_a_no_op(tmp_path):
    lock = SingleInstanceLock(tmp_path / "assistant.pid")
    lock.release()  # must not raise
