"""Per-user pending mutating operations — isolated by Google `sub` + session.

Atomic state machine: pending → executing → completed | ambiguous | cancelled.
Two concurrent confirms cannot both enter executing.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


DEFAULT_PENDING_TTL_S = 300.0  # 5 minutes
POST_SUCCESS_TTL_S = 120.0


class PendingConflict(Exception):
    """Refused to replace a pending or in-flight operation."""

    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__(state)

OpState = Literal["pending", "executing", "completed", "ambiguous", "cancelled"]


@dataclass
class PendingOperation:
    op_id: str
    user_sub: str
    kind: str  # calendar_create | calendar_cancel | calendar_reschedule | gmail_send
    summary_uk: str
    payload: dict[str, Any]
    session_id: str | None = None
    state: OpState = "pending"
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    executed: bool = False  # back-compat alias for state == completed
    execution_result: dict[str, Any] | None = None
    ambiguous_reason: str | None = None

    def is_expired(self, now: float | None = None) -> bool:
        if self.state in ("completed", "ambiguous"):
            return (now or time.time()) > self.expires_at
        if self.state == "executing":
            return False  # never expire mid-flight
        return (now or time.time()) > self.expires_at


class PendingStore:
    """In-process pending ops keyed by user_sub — never shared across accounts."""

    def __init__(self, ttl_seconds: float = DEFAULT_PENDING_TTL_S) -> None:
        self._ttl = ttl_seconds
        self._by_user: dict[str, PendingOperation] = {}
        self._lock = threading.RLock()

    def put(
        self,
        user_sub: str,
        kind: str,
        summary_uk: str,
        payload: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> PendingOperation:
        if not user_sub:
            raise ValueError("user_sub is required for pending operations")
        with self._lock:
            existing = self._by_user.get(user_sub)
            if (
                existing is not None
                and existing.user_sub == user_sub
                and existing.state in ("pending", "executing")
                and not (existing.state == "pending" and existing.is_expired())
            ):
                raise PendingConflict(existing.state)
            op = PendingOperation(
                op_id=str(uuid.uuid4()),
                user_sub=user_sub,
                kind=kind,
                summary_uk=summary_uk,
                payload=dict(payload),
                session_id=session_id,
                state="pending",
                expires_at=time.time() + self._ttl,
            )
            self._by_user[user_sub] = op
            return op

    def get(self, user_sub: str) -> PendingOperation | None:
        with self._lock:
            op, _reason = self._take_unlocked(user_sub)
            return op

    def get_with_reason(self, user_sub: str) -> tuple[PendingOperation | None, str]:
        """Return the live op, or why it is gone: pending_missing | pending_expired."""
        with self._lock:
            return self._take_unlocked(user_sub)

    def _get_unlocked(self, user_sub: str) -> PendingOperation | None:
        op, _reason = self._take_unlocked(user_sub)
        return op

    def _take_unlocked(self, user_sub: str) -> tuple[PendingOperation | None, str]:
        op = self._by_user.get(user_sub)
        if op is None:
            return None, "pending_missing"
        if op.user_sub != user_sub or op.state == "cancelled":
            self._by_user.pop(user_sub, None)
            return None, "pending_missing"
        if op.is_expired() and op.state == "pending":
            self._by_user.pop(user_sub, None)
            return None, "pending_expired"
        if op.is_expired() and op.state in ("completed", "ambiguous"):
            self._by_user.pop(user_sub, None)
            return None, "pending_expired"
        return op, ""

    def clear(self, user_sub: str) -> None:
        with self._lock:
            self._by_user.pop(user_sub, None)

    def begin_execute(self, user_sub: str, op_id: str) -> PendingOperation | None:
        """Atomically claim pending → executing. Returns None if another confirm won the race."""
        with self._lock:
            op = self._get_unlocked(user_sub)
            if op is None:
                return None
            if op.op_id != op_id or op.user_sub != user_sub:
                return None
            if op.state == "completed" and op.execution_result is not None:
                return op  # caller must treat as already done
            if op.state == "ambiguous":
                return op
            if op.state == "executing":
                return None  # concurrent confirm lost the race
            if op.state != "pending":
                return None
            op.state = "executing"
            return op

    def mark_executed(self, user_sub: str, result: dict[str, Any]) -> PendingOperation | None:
        with self._lock:
            op = self._by_user.get(user_sub)
            if op is None or op.user_sub != user_sub:
                return None
            op.state = "completed"
            op.executed = True
            op.execution_result = result
            op.expires_at = time.time() + POST_SUCCESS_TTL_S
            return op

    def mark_ambiguous(self, user_sub: str, reason: str) -> PendingOperation | None:
        """After uncertain network/API outcome — do NOT auto-retry the mutation."""
        with self._lock:
            op = self._by_user.get(user_sub)
            if op is None or op.user_sub != user_sub:
                return None
            op.state = "ambiguous"
            op.ambiguous_reason = reason
            op.expires_at = time.time() + POST_SUCCESS_TTL_S
            return op

    def mark_cancelled(self, user_sub: str) -> bool:
        """Cancel a not-yet-started op. In-flight API calls are left untouched."""
        with self._lock:
            op = self._by_user.get(user_sub)
            if op is None or op.user_sub != user_sub:
                return False
            if op.state == "executing":
                return False
            op.state = "cancelled"
            self._by_user.pop(user_sub, None)
            return True

    def has_pending(self, user_sub: str) -> bool:
        op = self.get(user_sub)
        return op is not None and op.state == "pending"
