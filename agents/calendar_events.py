"""Pure helpers over Google Calendar event dicts: snapshots, conflicts, recurrence, time bounds."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from integrations.google_errors import GoogleApiError


def _uncertain(exc: GoogleApiError) -> bool:
    return exc.code in {"network", "timeout", "google_unavailable"} or (
        exc.http_status is not None and exc.http_status >= 500
    )


def _google_event_gone(event: dict | None) -> bool:
    """Google soft-deletes: events.get still returns the row with status=cancelled."""
    if not event:
        return True
    return str(event.get("status") or "").strip().lower() == "cancelled"


def _scope(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    token = str(value).strip().lower()
    if token in {"instance", "this", "цей", "лише цей"}:
        return "instance"
    if token in {"series", "all", "уся", "вся", "серія", "всю серію"}:
        return "series"
    return None


def _is_recurring(event: dict) -> bool:
    return bool(event.get("recurrence") or event.get("recurringEventId"))


def _mutation_id(event: dict, scope: str | None) -> str:
    if scope == "series":
        return str(event.get("recurringEventId") or event["id"])
    return str(event["id"])


def _snapshot(event: dict) -> dict:
    start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date") or ""
    end = (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date") or ""
    return {
        "summary": event.get("summary") or "",
        "description": event.get("description") or "",
        "start": start,
        "end": end,
        "etag": event.get("etag") or "",
    }


def _conflicts(saved: dict, live: dict) -> bool:
    current = _snapshot(live)
    for key in ("summary", "description", "start", "end"):
        if (saved.get(key) or "") != (current.get(key) or ""):
            return True
    if saved.get("etag") and current.get("etag") and saved["etag"] != current["etag"]:
        return True
    return False


def _patch_applied(fresh: dict, patch: dict) -> bool:
    if "summary" in patch and (fresh.get("summary") or "") != patch["summary"]:
        return False
    if "description" in patch and (fresh.get("description") or "") != patch["description"]:
        return False
    if "start" in patch:
        got = (fresh.get("start") or {}).get("dateTime")
        if got != (patch["start"] or {}).get("dateTime"):
            return False
    if "end" in patch:
        got = (fresh.get("end") or {}).get("dateTime")
        if got != (patch["end"] or {}).get("dateTime"):
            return False
    return True



def _parse_instant(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def _timed_bounds(event: dict) -> tuple[datetime, datetime] | None:
    start_raw = (event.get("start") or {}).get("dateTime")
    end_raw = (event.get("end") or {}).get("dateTime")
    if not start_raw or not end_raw:
        return None
    return _parse_instant(str(start_raw)), _parse_instant(str(end_raw))


def _local_parts(event: dict, timezone: str) -> tuple[str, str] | None:
    bounds = _timed_bounds(event)
    if bounds is None:
        return None
    local = bounds[0].astimezone(ZoneInfo(timezone))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _is_past(event: dict, now: datetime, timezone: str) -> bool:
    start_raw = (event.get("start") or {}).get("dateTime")
    if start_raw:
        return _parse_instant(str(start_raw)) < now
    day = (event.get("start") or {}).get("date")
    if day:
        return str(day) < now.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d")
    return False
