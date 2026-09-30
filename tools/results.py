"""Structured tool results for Live Responses delegation."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from agents.types import AgentResult

ToolStatus = Literal[
    "ok",
    "confirmation_required",
    "completed",
    "not_found",
    "ambiguous",
    "cancelled",
    "rejected",
    "stale",
    "error",
    "needs_more_info",
    "auth_required",
    "permission_required",
    "permission_denied",
    "rate_limited",
    "success",
]


@dataclass
class ToolResult:
    ok: bool
    status: str
    message: str
    op_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "data": dict(self.data),
        }
        if self.op_id:
            body["op_id"] = self.op_id
        return body

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


_SUCCESSISH = frozenset({"success", "ok", "completed", "confirmation_required", "needs_more_info"})


def agent_result_to_tool_result(result: AgentResult) -> ToolResult:
    data = dict(result.data or {})
    op_id = data.get("op_id")
    status = result.status
    # Map domain statuses onto the Live-facing vocabulary while preserving originals.
    if status == "success":
        mapped = "completed" if data.get("pending_state") == "completed" or data.get("executed") else "ok"
    else:
        mapped = status
    ok = status in _SUCCESSISH and status not in ("error",)
    # confirmation_required is a successful prepare, not a mutation success.
    if status == "confirmation_required":
        ok = True
        mapped = "confirmation_required"
    elif status in ("error", "not_found", "ambiguous", "rate_limited", "permission_denied"):
        ok = False
        mapped = status
    return ToolResult(
        ok=ok,
        status=mapped,
        message=result.message,
        op_id=str(op_id) if op_id else None,
        data=data,
    )
