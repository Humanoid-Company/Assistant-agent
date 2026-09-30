"""Google Drive + Docs client for per-user agent notes (mockable).

v1: human-readable Doc + optional visible ``[id: agent_note_…]`` lines.
v2: Named Ranges as identity; no visible technical IDs in new notes.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from integrations.google_errors import GoogleApiError, map_google_error

logger = logging.getLogger(__name__)

NOTES_DOC_TITLE = "Нотатки від агента"
APP_PROP_KEY = "voice_agent_notes"
APP_PROP_VALUE = "true"
NOTE_ID_PREFIX = "agent_note_"

_HEADER_RE = re.compile(
    r"^##\s+(?P<ts>\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})\s+—\s+(?P<title>.+?)\s*$",
    re.MULTILINE,
)
_NOTE_ID_RE = re.compile(r"^\[id:\s*(?P<nid>agent_note_[0-9a-fA-F\-]+)\]\s*$", re.MULTILINE)
_NOTE_ID_LINE_RE = re.compile(r"(?m)^\[id:\s*(?P<nid>agent_note_[0-9a-fA-F\-]+)\]\s*\n?")

_LATEST_REFS = frozenset(
    {
        "остання",
        "останню",
        "останньої",
        "остання нотатка",
        "останню нотатку",
        "latest",
        "last",
        "last note",
        "the last note",
    }
)
_PREV_REFS = frozenset(
    {
        "передостання",
        "передостанню",
        "передостання нотатка",
        "передостанню нотатку",
        "previous",
        "prev",
        "second last",
        "penultimate",
    }
)


def utf16_len(text: str) -> int:
    """Google Docs indexes UTF-16 code units, not Python code points."""
    return len((text or "").encode("utf-16-le")) // 2


@dataclass(frozen=True)
class Note:
    note_id: str
    timestamp: str
    title: str
    content: str
    category: str | None = None
    start_index: int | None = None
    end_index: int | None = None
    named_range_id: str | None = None
    anchored: bool = False

    def preview(self, max_chars: int = 120) -> str:
        text = " ".join((self.content or "").split())
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "note_id": self.note_id,
            "timestamp": self.timestamp,
            "title": self.title,
            "content": self.content,
            "preview": self.preview(),
        }
        if self.category:
            out["category"] = self.category
        return out


class NotesAmbiguousError(ValueError):
    def __init__(self, message: str, matches: list[Note]) -> None:
        super().__init__(message)
        self.matches = matches


class NotesNotFoundError(ValueError):
    pass


class NotesConflictError(GoogleApiError):
    def __init__(self, message: str = "Документ змінився під час запису. Спробуй ще раз.") -> None:
        super().__init__(code="conflict", http_status=409, message=message)


class NotesClient(Protocol):
    def find_notes_documents(self) -> list[dict[str, Any]]: ...

    def create_notes_document(self, *, title: str = NOTES_DOC_TITLE) -> dict[str, Any]: ...

    def get_document(self, document_id: str) -> dict[str, Any]: ...

    def get_document_text(self, document_id: str) -> str: ...

    def batch_update(
        self,
        document_id: str,
        requests: list[dict[str, Any]],
        *,
        required_revision_id: str | None = None,
    ) -> dict[str, Any]: ...


class GoogleNotesClient:
    """Thin Drive v3 + Docs v1 wrapper. Credentials must include drive.file."""

    def __init__(self, credentials) -> None:
        self._drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._docs = build("docs", "v1", credentials=credentials, cache_discovery=False)

    def find_notes_documents(self) -> list[dict[str, Any]]:
        query = (
            f"appProperties has {{ key='{APP_PROP_KEY}' and value='{APP_PROP_VALUE}' }} "
            "and trashed=false "
            "and mimeType='application/vnd.google-apps.document'"
        )
        try:
            response = (
                self._drive.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="files(id,name,createdTime,appProperties)",
                    orderBy="createdTime",
                    pageSize=10,
                )
                .execute()
            )
            return list(response.get("files") or [])
        except Exception as exc:
            raise map_google_error(exc) from exc

    def create_notes_document(self, *, title: str = NOTES_DOC_TITLE) -> dict[str, Any]:
        body = {
            "name": title,
            "mimeType": "application/vnd.google-apps.document",
            "appProperties": {APP_PROP_KEY: APP_PROP_VALUE},
        }
        try:
            created = (
                self._drive.files()
                .create(body=body, fields="id,name,createdTime,appProperties")
                .execute()
            )
            document_id = created["id"]
            self.batch_update(
                document_id,
                [{"insertText": {"location": {"index": 1}, "text": f"# {NOTES_DOC_TITLE}\n\n"}}],
            )
            return created
        except Exception as exc:
            raise map_google_error(exc) from exc

    def get_document(self, document_id: str) -> dict[str, Any]:
        try:
            return self._docs.documents().get(documentId=document_id).execute()
        except Exception as exc:
            raise map_google_error(exc) from exc

    def get_document_text(self, document_id: str) -> str:
        return _extract_plain_text(self.get_document(document_id))

    def batch_update(
        self,
        document_id: str,
        requests: list[dict[str, Any]],
        *,
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        if not requests:
            return {}
        body: dict[str, Any] = {"requests": requests}
        if required_revision_id:
            body["writeControl"] = {"requiredRevisionId": required_revision_id}
        try:
            return self._docs.documents().batchUpdate(documentId=document_id, body=body).execute()
        except Exception as exc:
            mapped = map_google_error(exc)
            detail = str(exc).lower()
            if mapped.code == "conflict" or "revision" in detail:
                raise NotesConflictError() from exc
            raise mapped from exc


def _body_end_index(doc: dict[str, Any]) -> int:
    content = (doc.get("body") or {}).get("content") or []
    if not content:
        return 1
    return int(content[-1].get("endIndex") or 1)


def _extract_plain_text(doc: dict[str, Any]) -> str:
    chunks: list[str] = []
    for element in (doc.get("body") or {}).get("content") or []:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for pe in paragraph.get("elements") or []:
            text_run = pe.get("textRun")
            if text_run and isinstance(text_run.get("content"), str):
                chunks.append(text_run["content"])
    return "".join(chunks)


def _named_ranges_map(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    named = doc.get("namedRanges") or {}
    for name, payload in named.items():
        ranges = payload.get("namedRanges") or []
        if not ranges:
            continue
        first = ranges[0]
        ranges_list = first.get("ranges") or []
        if not ranges_list:
            continue
        seg = ranges_list[0]
        out[str(name)] = {
            "startIndex": int(seg.get("startIndex") or 0),
            "endIndex": int(seg.get("endIndex") or 0),
            "namedRangeId": first.get("namedRangeId"),
        }
    return out


def _docs_index_to_py(text: str, docs_index: int) -> int:
    target = max(0, int(docs_index) - 1)
    acc = 0
    for i, ch in enumerate(text):
        if acc == target:
            return i
        acc += utf16_len(ch)
        if acc > target:
            return i
    return len(text)


def _py_to_docs_index(text: str, py_index: int) -> int:
    return 1 + utf16_len(text[: max(0, min(py_index, len(text)))])


def format_note_block(
    *,
    content: str,
    title: str,
    timestamp: datetime,
    note_id: str | None = None,
    include_visible_id: bool = False,
) -> tuple[str, Note]:
    """Build a human-readable note block. v2 omits visible ``[id:]`` lines."""
    nid = note_id or f"{NOTE_ID_PREFIX}{uuid.uuid4().hex}"
    ts = timestamp.strftime("%d.%m.%Y %H:%M")
    clean_title = (title or "Нотатка").strip() or "Нотатка"
    body = (content or "").rstrip()
    if include_visible_id:
        block = f"## {ts} — {clean_title}\n[id: {nid}]\n{body}\n\n"
    else:
        block = f"## {ts} — {clean_title}\n{body}\n\n"
    return block, Note(
        note_id=nid,
        timestamp=ts,
        title=clean_title,
        content=body,
        anchored=True,
    )


def parse_notes(document_text: str) -> list[Note]:
    """Parse note blocks from plain text (legacy + v2 visible format)."""
    text = document_text or ""
    matches = list(_HEADER_RE.finditer(text))
    if not matches:
        return []
    notes: list[Note] = []
    for index, match in enumerate(matches):
        start = match.start()
        body_start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw_body = text[body_start:end].strip("\n")
        note_id = ""
        id_match = _NOTE_ID_RE.search(raw_body)
        if id_match:
            note_id = id_match.group("nid")
            raw_body = (raw_body[: id_match.start()] + raw_body[id_match.end() :]).strip("\n")
        anchored = bool(note_id)
        if not note_id:
            note_id = f"{NOTE_ID_PREFIX}orphan_{index}"
        notes.append(
            Note(
                note_id=note_id,
                timestamp=match.group("ts"),
                title=match.group("title").strip(),
                content=raw_body.strip(),
                start_index=_py_to_docs_index(text, start),
                end_index=_py_to_docs_index(text, end),
                anchored=anchored,
            )
        )
    return notes


def parse_notes_from_document(doc: dict[str, Any]) -> list[Note]:
    """Parse notes and attach Named Range identity when present."""
    text = _extract_plain_text(doc)
    base = parse_notes(text)
    ranges = _named_ranges_map(doc)
    if not ranges:
        return base

    enriched: list[Note] = []
    used_names: set[str] = set()
    for note in base:
        matched_name: str | None = None
        matched_meta: dict[str, Any] | None = None
        if note.note_id in ranges and note.note_id not in used_names:
            matched_name = note.note_id
            matched_meta = ranges[note.note_id]
        else:
            best: tuple[int, str, dict[str, Any]] | None = None
            for name, meta in ranges.items():
                if name in used_names or not str(name).startswith(NOTE_ID_PREFIX):
                    continue
                rs, re_ = int(meta["startIndex"]), int(meta["endIndex"])
                ns, ne = note.start_index or 0, note.end_index or 0
                overlap = max(0, min(ne, re_) - max(ns, rs))
                if overlap <= 0:
                    continue
                if best is None or overlap > best[0]:
                    best = (overlap, name, meta)
            if best:
                matched_name, matched_meta = best[1], best[2]
        if matched_name and matched_meta:
            used_names.add(matched_name)
            enriched.append(
                replace(
                    note,
                    note_id=matched_name,
                    start_index=int(matched_meta["startIndex"]),
                    end_index=int(matched_meta["endIndex"]),
                    named_range_id=matched_meta.get("namedRangeId"),
                    anchored=True,
                )
            )
        else:
            enriched.append(note)
    return enriched


def search_notes(notes: list[Note], query: str, *, limit: int = 10) -> list[Note]:
    q = (query or "").strip().casefold()
    if not q:
        return []
    scored: list[tuple[int, int, Note]] = []
    for idx, note in enumerate(notes):
        hay = f"{note.title}\n{note.content}\n{note.timestamp}".casefold()
        if q not in hay:
            continue
        title_hit = 2 if q in note.title.casefold() else 0
        scored.append((title_hit, idx, note))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[: max(1, limit)]]


def resolve_note_targets(
    notes: list[Note],
    *,
    note_id: str | None = None,
    query: str | None = None,
    target: str | None = None,
) -> list[Note]:
    if note_id:
        nid = str(note_id).strip()
        return [n for n in notes if n.note_id == nid]

    ref = (target or query or "").strip()
    if not ref:
        return []
    folded = ref.casefold()
    if folded in _LATEST_REFS:
        return [notes[-1]] if notes else []
    if folded in _PREV_REFS:
        return [notes[-2]] if len(notes) >= 2 else []

    exact = [n for n in notes if n.title.casefold() == folded]
    if exact:
        return exact
    return search_notes(notes, ref, limit=20)


def filter_notes_by_date(
    notes: list[Note],
    *,
    date_filter: str | None = None,
    date: str | None = None,
    timezone_name: str = "Europe/Kyiv",
) -> list[Note]:
    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date()
    wanted: set[str] = set()
    df = (date_filter or "").strip().lower()
    if df in ("today", "сьогодні"):
        wanted.add(today.strftime("%d.%m.%Y"))
    elif df in ("yesterday", "вчора"):
        wanted.add((today - timedelta(days=1)).strftime("%d.%m.%Y"))
    raw_date = (date or "").strip()
    if raw_date:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            y, m, d = raw_date.split("-")
            wanted.add(f"{d}.{m}.{y}")
        elif re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", raw_date):
            wanted.add(raw_date)
    if not wanted:
        return notes
    return [n for n in notes if n.timestamp.split(" ")[0] in wanted]


class NotesService:
    """Business logic — ensure document, migrate, CRUD, search, count."""

    def __init__(self, client: NotesClient, *, timezone: str = "Europe/Kyiv") -> None:
        self._client = client
        self._tz = ZoneInfo(timezone)
        self._timezone_name = timezone
        self._ensure_lock = threading.Lock()
        self._cached_doc_id: str | None = None
        self._migrated_docs: set[str] = set()

    def ensure_notes_document(self) -> str:
        with self._ensure_lock:
            if self._cached_doc_id:
                return self._cached_doc_id
            existing = self._client.find_notes_documents()
            if existing:
                chosen = existing[0]
                doc_id = str(chosen["id"])
                if len(existing) > 1:
                    logger.warning(
                        "notes.duplicate_documents count=%s using_id=%s…",
                        len(existing),
                        doc_id[:8],
                    )
                self._cached_doc_id = doc_id
                return doc_id
            created = self._client.create_notes_document()
            again = self._client.find_notes_documents()
            doc_id = str(again[0]["id"]) if again else str(created["id"])
            self._cached_doc_id = doc_id
            logger.info("notes.document_ensured document_id=%s…", doc_id[:8])
            return doc_id

    def invalidate_cache(self) -> None:
        self._cached_doc_id = None

    def _load_doc(self, document_id: str) -> dict[str, Any]:
        return self._client.get_document(document_id)

    def _with_retry(self, document_id: str, mutator) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(2):
            doc = self._load_doc(document_id)
            revision = doc.get("revisionId")
            try:
                requests = mutator(doc)
            except NotesNotFoundError:
                raise
            if not requests:
                return doc
            try:
                self._client.batch_update(
                    document_id,
                    requests,
                    required_revision_id=str(revision) if revision else None,
                )
                return self._load_doc(document_id)
            except NotesConflictError as exc:
                last_exc = exc
                logger.warning(
                    "notes.revision_conflict attempt=%s doc=%s…",
                    attempt + 1,
                    document_id[:8],
                )
                continue
        raise last_exc or NotesConflictError()

    def migrate_legacy_ids(self, document_id: str | None = None) -> int:
        """Create Named Ranges for legacy ``[id:]`` lines and strip visible IDs."""
        document_id = document_id or self.ensure_notes_document()
        if document_id in self._migrated_docs:
            return 0
        created_count = {"n": 0}

        def build(doc: dict[str, Any]) -> list[dict[str, Any]]:
            text = _extract_plain_text(doc)
            existing = _named_ranges_map(doc)
            requests: list[dict[str, Any]] = []
            matches = list(_NOTE_ID_LINE_RE.finditer(text))
            for match in reversed(matches):
                nid = match.group("nid")
                headers = list(_HEADER_RE.finditer(text))
                block_start = 0
                block_end = len(text)
                for i, h in enumerate(headers):
                    nxt = headers[i + 1].start() if i + 1 < len(headers) else len(text)
                    if h.start() <= match.start() < nxt:
                        block_start = h.start()
                        block_end = nxt
                        break
                start_idx = _py_to_docs_index(text, block_start)
                end_idx = _py_to_docs_index(text, block_end)
                if nid not in existing:
                    requests.append(
                        {
                            "createNamedRange": {
                                "name": nid,
                                "range": {"startIndex": start_idx, "endIndex": end_idx},
                            }
                        }
                    )
                    created_count["n"] += 1
                id_start = _py_to_docs_index(text, match.start())
                id_end = _py_to_docs_index(text, match.end())
                if id_end > id_start:
                    requests.append(
                        {
                            "deleteContentRange": {
                                "range": {"startIndex": id_start, "endIndex": id_end},
                            }
                        }
                    )
            return requests

        self._with_retry(document_id, build)
        self._migrated_docs.add(document_id)
        if created_count["n"]:
            logger.info(
                "notes.legacy_migrated document_id=%s… count=%s",
                document_id[:8],
                created_count["n"],
            )
        return created_count["n"]

    def _ensure_ready(self) -> tuple[str, dict[str, Any], list[Note]]:
        document_id = self.ensure_notes_document()
        try:
            self.migrate_legacy_ids(document_id)
        except GoogleApiError as exc:
            if exc.code != "not_found":
                logger.warning("notes.migration_skipped error=%s", exc.code)
        try:
            doc = self._load_doc(document_id)
        except GoogleApiError as exc:
            if exc.code == "not_found":
                self.invalidate_cache()
                document_id = self.ensure_notes_document()
                doc = self._load_doc(document_id)
            else:
                raise
        return document_id, doc, parse_notes_from_document(doc)

    def add_note(
        self,
        content: str,
        *,
        title: str | None = None,
        category: str | None = None,
    ) -> tuple[str, Note]:
        body = (content or "").strip()
        if not body:
            raise ValueError("empty_content")
        heading = (title or category or "Нотатка").strip() or "Нотатка"
        document_id = self.ensure_notes_document()
        try:
            self.migrate_legacy_ids(document_id)
        except GoogleApiError:
            pass
        now = datetime.now(self._tz)
        block, note = format_note_block(content=body, title=heading, timestamp=now)
        note = replace(note, category=category)

        def build(doc: dict[str, Any]) -> list[dict[str, Any]]:
            text = _extract_plain_text(doc)
            end_index = _body_end_index(doc)
            insert_at = max(1, end_index - 1)
            if not text.strip():
                seed = f"# {NOTES_DOC_TITLE}\n\n{block}"
                reqs: list[dict[str, Any]] = []
                if end_index > 2:
                    reqs.append(
                        {
                            "deleteContentRange": {
                                "range": {"startIndex": 1, "endIndex": end_index - 1},
                            }
                        }
                    )
                reqs.append({"insertText": {"location": {"index": 1}, "text": seed}})
                start_idx = 1 + utf16_len(f"# {NOTES_DOC_TITLE}\n\n")
                end_idx = start_idx + utf16_len(block)
                reqs.append(
                    {
                        "createNamedRange": {
                            "name": note.note_id,
                            "range": {"startIndex": start_idx, "endIndex": end_idx},
                        }
                    }
                )
                return reqs
            start_idx = insert_at
            end_idx = insert_at + utf16_len(block)
            return [
                {"insertText": {"location": {"index": insert_at}, "text": block}},
                {
                    "createNamedRange": {
                        "name": note.note_id,
                        "range": {"startIndex": start_idx, "endIndex": end_idx},
                    }
                },
            ]

        self._with_retry(document_id, build)
        return document_id, note

    def list_notes(
        self,
        *,
        limit: int = 10,
        date_filter: str | None = None,
        date: str | None = None,
    ) -> tuple[str, list[Note], int]:
        document_id, _doc, notes = self._ensure_ready()
        notes = filter_notes_by_date(
            notes,
            date_filter=date_filter,
            date=date,
            timezone_name=self._timezone_name,
        )
        total = len(notes)
        limit = max(1, min(int(limit or 10), 50))
        return document_id, notes[-limit:], total

    def count_notes(self) -> tuple[str, int]:
        document_id, _doc, notes = self._ensure_ready()
        return document_id, len(notes)

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        date_filter: str | None = None,
        date: str | None = None,
    ) -> tuple[str, list[Note]]:
        document_id, _doc, notes = self._ensure_ready()
        notes = filter_notes_by_date(
            notes,
            date_filter=date_filter,
            date=date,
            timezone_name=self._timezone_name,
        )
        limit = max(1, min(int(limit or 10), 50))
        return document_id, search_notes(notes, query, limit=limit)

    def _require_single_target(
        self,
        notes: list[Note],
        *,
        note_id: str | None,
        query: str | None,
        target: str | None,
    ) -> Note:
        hits = resolve_note_targets(notes, note_id=note_id, query=query, target=target)
        if not hits:
            raise NotesNotFoundError("not_found")
        if len(hits) > 1:
            raise NotesAmbiguousError("ambiguous", hits)
        note = hits[0]
        if note.note_id.startswith(f"{NOTE_ID_PREFIX}orphan_"):
            raise NotesNotFoundError("stale_reference")
        return note

    def update_note(
        self,
        *,
        note_id: str | None = None,
        query: str | None = None,
        target: str | None = None,
        content: str | None = None,
        title: str | None = None,
        append_text: str | None = None,
    ) -> tuple[str, Note]:
        if content is None and title is None and append_text is None:
            raise ValueError("empty_update")
        if append_text is not None and not str(append_text).strip():
            raise ValueError("empty_append")
        if content is not None and not str(content).strip() and append_text is None and title is None:
            raise ValueError("empty_content")

        document_id, _doc, notes = self._ensure_ready()
        current = self._require_single_target(
            notes, note_id=note_id, query=query, target=target
        )
        new_title = (title if title is not None else current.title).strip() or current.title
        if append_text is not None:
            addition = str(append_text).strip()
            base = current.content.rstrip()
            new_content = f"{base}\n{addition}" if base else addition
        elif content is not None:
            new_content = str(content).rstrip()
        else:
            new_content = current.content

        block, _tmp = format_note_block(
            content=new_content,
            title=new_title,
            timestamp=datetime.strptime(current.timestamp, "%d.%m.%Y %H:%M").replace(tzinfo=self._tz),
            note_id=current.note_id,
        )
        updated = Note(
            note_id=current.note_id,
            timestamp=current.timestamp,
            title=new_title,
            content=new_content,
            category=current.category,
            anchored=True,
        )

        def build(doc: dict[str, Any]) -> list[dict[str, Any]]:
            ranges = _named_ranges_map(doc)
            meta = ranges.get(current.note_id)
            if not meta:
                parsed = parse_notes_from_document(doc)
                hit = next((n for n in parsed if n.note_id == current.note_id), None)
                if hit is None or hit.start_index is None or hit.end_index is None:
                    raise NotesNotFoundError("stale_reference")
                start_i, end_i = hit.start_index, hit.end_index
                named_range_id = hit.named_range_id
            else:
                start_i, end_i = int(meta["startIndex"]), int(meta["endIndex"])
                named_range_id = meta.get("namedRangeId")
            reqs: list[dict[str, Any]] = []
            if named_range_id:
                reqs.append({"deleteNamedRange": {"namedRangeId": named_range_id}})
            if end_i > start_i:
                reqs.append(
                    {
                        "deleteContentRange": {
                            "range": {"startIndex": start_i, "endIndex": end_i},
                        }
                    }
                )
            reqs.append({"insertText": {"location": {"index": start_i}, "text": block}})
            reqs.append(
                {
                    "createNamedRange": {
                        "name": current.note_id,
                        "range": {
                            "startIndex": start_i,
                            "endIndex": start_i + utf16_len(block),
                        },
                    }
                }
            )
            return reqs

        self._with_retry(document_id, build)
        return document_id, updated

    def delete_note(
        self,
        *,
        note_id: str | None = None,
        query: str | None = None,
        target: str | None = None,
    ) -> tuple[str, Note]:
        document_id, _doc, notes = self._ensure_ready()
        current = self._require_single_target(
            notes, note_id=note_id, query=query, target=target
        )

        def build(doc: dict[str, Any]) -> list[dict[str, Any]]:
            ranges = _named_ranges_map(doc)
            meta = ranges.get(current.note_id)
            if not meta:
                parsed = parse_notes_from_document(doc)
                hit = next((n for n in parsed if n.note_id == current.note_id), None)
                if hit is None or hit.start_index is None or hit.end_index is None:
                    raise NotesNotFoundError("stale_reference")
                start_i, end_i = hit.start_index, hit.end_index
                named_range_id = hit.named_range_id
            else:
                start_i, end_i = int(meta["startIndex"]), int(meta["endIndex"])
                named_range_id = meta.get("namedRangeId")
            reqs: list[dict[str, Any]] = []
            if named_range_id:
                reqs.append({"deleteNamedRange": {"namedRangeId": named_range_id}})
            if end_i > start_i:
                reqs.append(
                    {
                        "deleteContentRange": {
                            "range": {"startIndex": start_i, "endIndex": end_i},
                        }
                    }
                )
            return reqs

        self._with_retry(document_id, build)
        self._collapse_blank_lines(document_id)
        return document_id, current

    def _collapse_blank_lines(self, document_id: str) -> None:
        """Remove runs of 3+ newlines without rewriting the whole doc when possible."""

        def build(doc: dict[str, Any]) -> list[dict[str, Any]]:
            text = _extract_plain_text(doc)
            cleaned = re.sub(r"\n{3,}", "\n\n", text)
            if cleaned == text:
                return []
            # Surgical: only when simple collapse — full rewrite would drop named ranges.
            # Prefer leave mild extra blanks over destroying anchors.
            return []

        try:
            self._with_retry(document_id, build)
        except Exception:
            pass


class FakeNotesClient:
    """In-memory Drive/Docs stand-in with Named Ranges + revision conflicts."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.texts: dict[str, str] = {}
        self.named_ranges: dict[str, dict[str, dict[str, Any]]] = {}
        self.revision: dict[str, int] = {}
        self.create_calls = 0
        self.fail_next: str | None = None
        self.conflict_once: bool = False
        self.batch_calls = 0

    def find_notes_documents(self) -> list[dict[str, Any]]:
        if self.fail_next == "find":
            self.fail_next = None
            raise GoogleApiError("google_unavailable", 500, "Google API тимчасово недоступний.")
        docs = [
            f
            for f in self.files.values()
            if (f.get("appProperties") or {}).get(APP_PROP_KEY) == APP_PROP_VALUE
            and not f.get("trashed")
        ]
        docs.sort(key=lambda item: item.get("createdTime") or "")
        return docs

    def create_notes_document(self, *, title: str = NOTES_DOC_TITLE) -> dict[str, Any]:
        if self.fail_next == "create":
            self.fail_next = None
            raise GoogleApiError("api_disabled", 403, "Google Drive API не увімкнено.")
        self.create_calls += 1
        doc_id = f"doc-{uuid.uuid4().hex[:8]}"
        meta = {
            "id": doc_id,
            "name": title,
            "createdTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "appProperties": {APP_PROP_KEY: APP_PROP_VALUE},
        }
        self.files[doc_id] = meta
        self.texts[doc_id] = f"# {NOTES_DOC_TITLE}\n\n"
        self.named_ranges[doc_id] = {}
        self.revision[doc_id] = 1
        return meta

    def get_document(self, document_id: str) -> dict[str, Any]:
        if self.fail_next == "get":
            self.fail_next = None
            raise GoogleApiError("google_unavailable", 500, "Google API тимчасово недоступний.")
        if document_id not in self.texts:
            raise GoogleApiError("not_found", 404, "Ресурс не знайдено в Google (404).")
        text = self.texts[document_id]
        body_content = [
            {
                "startIndex": 1,
                "endIndex": 1 + utf16_len(text) + 1,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 1,
                            "endIndex": 1 + utf16_len(text),
                            "textRun": {"content": text},
                        }
                    ]
                },
            }
        ]
        named: dict[str, Any] = {}
        for name, meta in self.named_ranges.get(document_id, {}).items():
            named[name] = {
                "namedRanges": [
                    {
                        "namedRangeId": meta.get("namedRangeId"),
                        "name": name,
                        "ranges": [
                            {
                                "startIndex": meta["startIndex"],
                                "endIndex": meta["endIndex"],
                            }
                        ],
                    }
                ]
            }
        return {
            "documentId": document_id,
            "revisionId": str(self.revision.get(document_id, 1)),
            "body": {"content": body_content},
            "namedRanges": named,
        }

    def get_document_text(self, document_id: str) -> str:
        return _extract_plain_text(self.get_document(document_id))

    def batch_update(
        self,
        document_id: str,
        requests: list[dict[str, Any]],
        *,
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self.batch_calls += 1
        if document_id not in self.texts:
            raise GoogleApiError("not_found", 404, "Ресурс не знайдено в Google (404).")
        if self.conflict_once:
            self.conflict_once = False
            raise NotesConflictError()
        if required_revision_id is not None and required_revision_id != str(
            self.revision.get(document_id, 1)
        ):
            raise NotesConflictError()

        text = self.texts[document_id]
        ranges = dict(self.named_ranges.get(document_id, {}))

        def apply_delete_range(start: int, end: int) -> None:
            nonlocal text, ranges
            a = _docs_index_to_py(text, start)
            b = _docs_index_to_py(text, end)
            deleted_u16 = utf16_len(text[a:b])
            text = text[:a] + text[b:]
            new_ranges: dict[str, dict[str, Any]] = {}
            for name, meta in ranges.items():
                rs, re_ = int(meta["startIndex"]), int(meta["endIndex"])
                if re_ <= start:
                    new_ranges[name] = meta
                elif rs >= end:
                    new_ranges[name] = {
                        **meta,
                        "startIndex": rs - deleted_u16,
                        "endIndex": re_ - deleted_u16,
                    }
                else:
                    # Range overlaps deleted span — shrink (Docs adjusts named ranges).
                    new_start = rs
                    new_end = re_ - deleted_u16
                    if new_end > new_start:
                        new_ranges[name] = {
                            **meta,
                            "startIndex": new_start,
                            "endIndex": new_end,
                        }
            ranges = new_ranges

        def apply_insert(index: int, chunk: str) -> None:
            nonlocal text, ranges
            pos = _docs_index_to_py(text, index)
            inserted = utf16_len(chunk)
            text = text[:pos] + chunk + text[pos:]
            new_ranges: dict[str, dict[str, Any]] = {}
            for name, meta in ranges.items():
                rs, re_ = int(meta["startIndex"]), int(meta["endIndex"])
                if re_ <= index:
                    new_ranges[name] = meta
                elif rs >= index:
                    new_ranges[name] = {
                        **meta,
                        "startIndex": rs + inserted,
                        "endIndex": re_ + inserted,
                    }
                else:
                    new_ranges[name] = {**meta, "endIndex": re_ + inserted}
            ranges = new_ranges

        for req in requests:
            if "deleteNamedRange" in req:
                nrid = req["deleteNamedRange"]["namedRangeId"]
                ranges = {n: m for n, m in ranges.items() if m.get("namedRangeId") != nrid}
            elif "deleteContentRange" in req:
                r = req["deleteContentRange"]["range"]
                apply_delete_range(int(r["startIndex"]), int(r["endIndex"]))
            elif "insertText" in req:
                loc = req["insertText"]["location"]["index"]
                apply_insert(int(loc), req["insertText"]["text"])
            elif "createNamedRange" in req:
                cn = req["createNamedRange"]
                name = cn["name"]
                r = cn["range"]
                ranges[name] = {
                    "startIndex": int(r["startIndex"]),
                    "endIndex": int(r["endIndex"]),
                    "namedRangeId": f"nr-{uuid.uuid4().hex[:8]}",
                }

        self.texts[document_id] = text
        self.named_ranges[document_id] = ranges
        self.revision[document_id] = self.revision.get(document_id, 1) + 1
        return {"replies": []}

    def set_document_text(self, document_id: str, text: str) -> None:
        cur = self.texts.get(document_id, "")
        end = max(2, 1 + utf16_len(cur))
        reqs: list[dict[str, Any]] = []
        if utf16_len(cur) > 0:
            reqs.append(
                {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end}}}
            )
        reqs.append({"insertText": {"location": {"index": 1}, "text": text}})
        self.batch_update(document_id, reqs)

    def append_text(self, document_id: str, text: str) -> None:
        end = 1 + utf16_len(self.texts.get(document_id, ""))
        self.batch_update(
            document_id,
            [{"insertText": {"location": {"index": end}, "text": text}}],
        )
