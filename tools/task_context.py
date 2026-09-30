"""Lightweight task revision tracking for delegated backend work."""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class TaskContext:
    revision: int
    delegation_id: str | None = None


class TaskRevisionTracker:
    """Monotonic revision so superseded backend work can be recognized as stale.

    Does not replace PendingStore op_id checks — it is an additional correlation layer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._op_revisions: dict[str, int] = {}
        self._delegation_revision: dict[str, int] = {}

    @property
    def current(self) -> int:
        with self._lock:
            return self._revision

    def bump(self, *, reason: str = "") -> int:
        del reason
        with self._lock:
            self._revision += 1
            return self._revision

    def bind_op(self, op_id: str, revision: int | None = None) -> int:
        with self._lock:
            rev = self._revision if revision is None else revision
            self._op_revisions[op_id] = rev
            return rev

    def bind_delegation(self, delegation_id: str, revision: int | None = None) -> int:
        with self._lock:
            rev = self._revision if revision is None else revision
            self._delegation_revision[delegation_id] = rev
            return rev

    def is_current(self, revision: int) -> bool:
        with self._lock:
            return revision == self._revision

    def is_op_current(self, op_id: str) -> bool:
        with self._lock:
            bound = self._op_revisions.get(op_id)
            if bound is None:
                return True  # unknown op — let PendingStore decide
            return bound == self._revision

    def mark_stale_except(self, keep_op_id: str | None = None) -> int:
        """Bump so all previously bound ops become stale; optionally re-bind one."""
        with self._lock:
            self._revision += 1
            if keep_op_id and keep_op_id in self._op_revisions:
                self._op_revisions[keep_op_id] = self._revision
            return self._revision
