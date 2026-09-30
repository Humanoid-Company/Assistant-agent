"""Google Docs personal notes — v1 + v2 (Named Ranges / edit / delete)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from agents.types import AgentResult
from auth.scopes import NOTES_SCOPES
from integrations.google_notes import (
    APP_PROP_KEY,
    APP_PROP_VALUE,
    FakeNotesClient,
    NotesService,
    format_note_block,
    parse_notes,
    resolve_note_targets,
    search_notes,
    utf16_len,
)
from tests.helpers_google import CALENDAR_ONLY_SCOPES, NOTES_ONLY_SCOPES, build_test_router
from tools.calendar_tools import CalendarToolWrappers
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.gmail_tools import GmailToolWrappers
from tools.live_schemas import LIVE_BACKEND_TOOLS
from tools.notes_tools import NotesToolWrappers
from tools.results import agent_result_to_tool_result
from tools.task_context import TaskRevisionTracker


def test_utf16_len_cyrillic_and_emoji():
    assert utf16_len("abc") == 3
    assert utf16_len("її") == 2
    assert utf16_len("Україна") == len("Україна")
    # Emoji outside BMP → 2 UTF-16 code units each.
    assert utf16_len("🙂") == 2
    assert utf16_len("а🙂б") == 4
    mixed = "Нотатка 🙂 про Лесика"
    assert utf16_len(mixed) == len(mixed.encode("utf-16-le")) // 2


def test_ensure_creates_document_with_app_properties():
    client = FakeNotesClient()
    service = NotesService(client)
    doc_id = service.ensure_notes_document()
    assert doc_id in client.files
    assert client.create_calls == 1
    props = client.files[doc_id]["appProperties"]
    assert props[APP_PROP_KEY] == APP_PROP_VALUE
    assert "Нотатки від агента" in client.texts[doc_id]


def test_ensure_finds_existing_and_does_not_duplicate():
    client = FakeNotesClient()
    service = NotesService(client)
    first = service.ensure_notes_document()
    service.invalidate_cache()
    second = service.ensure_notes_document()
    assert first == second
    assert client.create_calls == 1


def test_duplicate_race_prefers_oldest():
    client = FakeNotesClient()
    a = client.create_notes_document()
    b = client.create_notes_document()
    assert client.create_calls == 2
    service = NotesService(client)
    chosen = service.ensure_notes_document()
    assert chosen == a["id"]
    assert chosen != b["id"]
    assert client.create_calls == 2


def test_add_note_creates_named_range_without_visible_id():
    client = FakeNotesClient()
    service = NotesService(client, timezone="Europe/Kyiv")
    doc_id, note = service.add_note("Подзвонити Андрію", title="Нотатка")
    text = client.texts[doc_id]
    assert "Подзвонити Андрію" in text
    assert "— Нотатка" in text
    assert note.note_id.startswith("agent_note_")
    assert f"[id: {note.note_id}]" not in text
    assert "[id:" not in text
    assert note.note_id in client.named_ranges[doc_id]
    service.add_note("Ідея про Telegram", title="Ідея")
    assert client.create_calls == 1
    assert client.texts[doc_id].count("## ") == 2
    assert "Telegram" in client.texts[doc_id]


def test_format_and_parse_roundtrip_multiline_v2():
    block, note = format_note_block(
        content="рядок1\nрядок2",
        title="Ідея",
        timestamp=datetime(2026, 9, 30, 14, 25, tzinfo=ZoneInfo("Europe/Kyiv")),
        note_id="agent_note_abc",
    )
    assert "[id:" not in block
    doc = f"# Нотатки від агента\n\n{block}"
    parsed = parse_notes(doc)
    assert len(parsed) == 1
    assert parsed[0].title == "Ідея"
    assert parsed[0].content == "рядок1\nрядок2"
    assert parsed[0].timestamp == "30.09.2026 14:25"


def test_legacy_parse_still_reads_visible_id():
    doc = (
        "# Нотатки від агента\n\n"
        "## 30.09.2026 15:23 — Дзвінок Лесику\n"
        "[id: agent_note_d098]\n"
        "Подзвонити завтра Лесику.\n\n"
    )
    notes = parse_notes(doc)
    assert len(notes) == 1
    assert notes[0].note_id == "agent_note_d098"
    assert notes[0].content == "Подзвонити завтра Лесику."
    assert notes[0].anchored


def test_legacy_migration_creates_range_and_strips_id():
    client = FakeNotesClient()
    meta = client.create_notes_document()
    doc_id = meta["id"]
    legacy = (
        "# Нотатки від агента\n\n"
        "## 30.09.2026 15:23 — Дзвінок Лесику\n"
        "[id: agent_note_d098abcdef]\n"
        "Подзвонити завтра Лесику.\n\n"
    )
    client.texts[doc_id] = legacy
    service = NotesService(client)
    service._cached_doc_id = doc_id
    n = service.migrate_legacy_ids(doc_id)
    assert n == 1
    text = client.texts[doc_id]
    assert "[id:" not in text
    assert "Подзвонити завтра Лесику." in text
    assert "Дзвінок Лесику" in text
    assert "agent_note_d098abcdef" in client.named_ranges[doc_id]
    # Idempotent
    assert service.migrate_legacy_ids(doc_id) == 0
    assert text.count("Подзвонити завтра Лесику.") == 1


def test_migration_does_not_duplicate_named_range():
    client = FakeNotesClient()
    meta = client.create_notes_document()
    doc_id = meta["id"]
    client.texts[doc_id] = (
        "# Нотатки від агента\n\n"
        "## 01.01.2026 10:00 — Ідея\n"
        "[id: agent_note_dup1]\n"
        "Текст\n\n"
    )
    service = NotesService(client)
    service._cached_doc_id = doc_id
    service.migrate_legacy_ids(doc_id)
    service._migrated_docs.discard(doc_id)
    # Re-inject visible id to force another pass while range exists.
    client.texts[doc_id] = (
        "# Нотатки від агента\n\n"
        "## 01.01.2026 10:00 — Ідея\n"
        "[id: agent_note_dup1]\n"
        "Текст\n\n"
    )
    # Range still present from first migration — second pass must not create a second key.
    before = dict(client.named_ranges[doc_id])
    service.migrate_legacy_ids(doc_id)
    assert list(client.named_ranges[doc_id].keys()) == list(before.keys())


def test_read_notes_respects_limit_and_total():
    client = FakeNotesClient()
    service = NotesService(client)
    for i in range(5):
        service.add_note(f"нотатка номер {i}", title=f"N{i}")
    _doc, notes, total = service.list_notes(limit=2)
    assert total == 5
    assert len(notes) == 2
    assert notes[-1].content.endswith("4")


def test_search_notes_case_insensitive_and_empty():
    notes = parse_notes(
        "## 01.01.2026 10:00 — Ідея\nДодати Telegram\n\n"
        "## 01.01.2026 11:00 — Нотатка\nПодзвонити Андрію\n\n"
    )
    hits = search_notes(notes, "telegram", limit=5)
    assert len(hits) == 1
    assert "Telegram" in hits[0].content
    assert search_notes(notes, "немаєтакого", limit=5) == []


def test_resolve_latest_and_previous():
    notes = parse_notes(
        "## 01.01.2026 10:00 — A\none\n\n"
        "## 01.01.2026 11:00 — B\ntwo\n\n"
        "## 01.01.2026 12:00 — C\nthree\n\n"
    )
    assert resolve_note_targets(notes, target="остання")[0].title == "C"
    assert resolve_note_targets(notes, target="передостання")[0].title == "B"


def test_update_content_title_append_delete(tmp_path):
    router, *_ = build_test_router(tmp_path)
    client: FakeNotesClient = router._test_notes_client  # type: ignore[attr-defined]

    a = router.notes_action(action="add", content="Подзвонити Лесику завтра", title="Дзвінок Лесику")
    b = router.notes_action(action="add", content="Агент з рухами", title="Ідея агента")
    assert a.status == "success"
    note_a = a.data["note_id"]
    assert "[id:" not in client.texts[a.data["document_id"]]

    upd = router.notes_action(
        action="update",
        note_id=note_a,
        content="Подзвонити Лесику в п'ятницю",
    )
    assert upd.status == "success"
    assert upd.data["action"] == "updated"
    assert "п'ятницю" in upd.data["content"]
    assert client.texts[a.data["document_id"]].count("## ") == 2

    ap = router.notes_action(
        action="append",
        note_id=b.data["note_id"],
        append_text="і вміти плавати",
    )
    assert ap.status == "success"
    assert "плавати" in ap.data["content"]
    assert "Агент з рухами" in ap.data["content"]

    ren = router.notes_action(
        action="update",
        note_id=b.data["note_id"],
        title="Project Phoenix",
    )
    assert ren.status == "success"
    assert ren.data["title"] == "Project Phoenix"
    assert "плавати" in ren.data["content"]

    deleted = router.notes_action(action="delete", note_id=note_a)
    assert deleted.status == "success"
    assert deleted.data["action"] == "deleted"
    doc_id = a.data["document_id"]
    assert "Лесику" not in client.texts[doc_id]
    assert note_a not in client.named_ranges[doc_id]
    assert b.data["note_id"] in client.named_ranges[doc_id]


def test_update_missing_and_delete_missing(tmp_path):
    router, *_ = build_test_router(tmp_path)
    router.notes_action(action="add", content="єдина", title="One")
    miss = router.notes_action(action="update", note_id="agent_note_nope", content="x")
    assert miss.status == "not_found"
    miss_d = router.notes_action(action="delete", note_id="agent_note_nope")
    assert miss_d.status == "not_found"


def test_ambiguous_delete(tmp_path):
    router, *_ = build_test_router(tmp_path)
    router.notes_action(action="add", content="рухи агента", title="Ідея агента з рухами")
    router.notes_action(action="add", content="гра з агентом", title="Ідея гри з агентом")
    result = router.notes_action(action="delete", query="агент")
    assert result.status == "ambiguous"
    assert result.data["count"] >= 2


def test_count_notes(tmp_path):
    router, *_ = build_test_router(tmp_path)
    assert router.notes_action(action="count").data["total_count"] == 0
    router.notes_action(action="add", content="a", title="A")
    router.notes_action(action="add", content="b", title="B")
    counted = router.notes_action(action="count")
    assert counted.status == "success"
    assert counted.data["total_count"] == 2
    assert "2" in counted.message


def test_structured_search_and_read_include_note_id(tmp_path):
    router, *_ = build_test_router(tmp_path)
    added = router.notes_action(action="add", content="Telegram integration", title="Ідея")
    found = router.notes_action(action="search", query="telegram")
    assert found.data["notes"][0]["note_id"] == added.data["note_id"]
    latest = router.notes_action(action="read", limit=1)
    assert latest.data["notes"][0]["note_id"]
    assert latest.data["total_count"] == 1


def test_conversational_note_id_followup(tmp_path):
    router, *_ = build_test_router(tmp_path)
    router.notes_action(action="add", content="Project Phoenix base", title="Project Phoenix")
    found = router.notes_action(action="search", query="Phoenix")
    nid = found.data["notes"][0]["note_id"]
    ap = router.notes_action(action="append", note_id=nid, append_text="потрібен multiplayer")
    assert ap.status == "success"
    assert "multiplayer" in ap.data["content"]


def test_multiparagraph_update(tmp_path):
    router, *_ = build_test_router(tmp_path)
    added = router.notes_action(
        action="add",
        content="рядок1\nрядок2\nрядок3",
        title="Multi",
    )
    upd = router.notes_action(
        action="update",
        note_id=added.data["note_id"],
        content="рядок1\nрядок2 змінений\nрядок3",
    )
    assert "змінений" in upd.data["content"]


def test_revision_conflict_retries(tmp_path):
    notes = FakeNotesClient()
    router, *_ = build_test_router(tmp_path, notes=notes, scopes=NOTES_ONLY_SCOPES)
    added = router.notes_action(action="add", content="x", title="T")
    notes.conflict_once = True
    upd = router.notes_action(
        action="update",
        note_id=added.data["note_id"],
        content="y",
    )
    assert upd.status == "success"
    assert upd.data["content"] == "y"


def test_agent_add_read_search(tmp_path):
    router, *_ = build_test_router(tmp_path)
    notes_client: FakeNotesClient = router._test_notes_client  # type: ignore[attr-defined]

    added = router.notes_action(action="add", content="завтра подзвонити Андрію", title="Нотатка")
    assert added.status == "success"
    assert added.data["action"] == "note_added"
    assert "Андрію" in added.data["content"]
    assert notes_client.create_calls == 1
    assert "[id:" not in notes_client.texts[added.data["document_id"]]

    router.notes_action(action="add", content="додати агенту Telegram", category="Ідея")
    latest = router.notes_action(action="read", limit=2)
    assert latest.status == "success"
    assert latest.data["count"] == 2

    found = router.notes_action(action="search", query="telegram")
    assert found.status == "success"
    assert found.data["count"] == 1

    missing = router.notes_action(action="search", query="квантовий сир")
    assert missing.status == "not_found"


def test_empty_content_needs_more_info(tmp_path):
    router, *_ = build_test_router(tmp_path)
    result = router.notes_action(action="add", content="   ")
    assert result.status == "needs_more_info"


def test_missing_notes_scope_permission_required(tmp_path):
    router, *_ = build_test_router(tmp_path, scopes=CALENDAR_ONLY_SCOPES)
    result = router.notes_action(action="add", content="тест")
    assert result.status == "permission_required"
    assert any(s in (result.data.get("missing_scopes") or []) for s in NOTES_SCOPES)


def test_grant_notes_then_add(tmp_path):
    router, _cal, _mail, oauth, accounts = build_test_router(
        tmp_path, scopes=CALENDAR_ONLY_SCOPES
    )
    denied = router.notes_action(action="read", limit=3)
    assert denied.status == "permission_required"
    granted = router.grant_notes()
    assert granted.status == "success"
    assert granted.data.get("permission_granted") is True
    assert accounts.status().notes_ready
    ok = router.notes_action(action="add", content="після grant")
    assert ok.status == "success"


def test_google_api_error_mapped(tmp_path):
    notes = FakeNotesClient()
    notes.fail_next = "create"
    router, *_ = build_test_router(tmp_path, notes=notes, scopes=NOTES_ONLY_SCOPES)
    result = router.notes_action(action="add", content="x")
    assert result.status == "error"
    assert "Drive" in result.message or "API" in result.message


def test_live_tool_result_shape(tmp_path):
    router, *_ = build_test_router(tmp_path)
    wrappers = NotesToolWrappers(router.notes_action)
    ex = ToolExecutor(
        calendar=CalendarToolWrappers(router.calendar_action),
        gmail=GmailToolWrappers(router.gmail_action),
        notes=wrappers,
        revisions=TaskRevisionTracker(),
    )
    result = asyncio.run(
        ex.execute(
            "notes_add",
            {"content": "купити кабель", "title": "Покупки"},
            ToolExecutionContext(session_id="sess-notes"),
        )
    )
    assert result.ok
    assert result.data.get("action") == "note_added"
    assert result.data.get("note_id")

    upd = asyncio.run(
        ex.execute(
            "notes_update",
            {"note_id": result.data["note_id"], "content": "купити USB-C"},
            ToolExecutionContext(session_id="sess-notes"),
        )
    )
    assert upd.ok
    deleted = asyncio.run(
        ex.execute(
            "notes_delete",
            {"note_id": result.data["note_id"]},
            ToolExecutionContext(session_id="sess-notes"),
        )
    )
    assert deleted.ok
    counted = asyncio.run(
        ex.execute("notes_count", {}, ToolExecutionContext(session_id="sess-notes"))
    )
    assert counted.ok
    assert counted.data.get("total_count") == 0

    agent = AgentResult("success", "ok", {"action": "note_added", "success": True})
    assert agent_result_to_tool_result(agent).ok


def test_live_schemas_include_notes_v2_tools():
    names = {t["name"] for t in LIVE_BACKEND_TOOLS}
    assert "notes_add" in names
    assert "notes_read" in names
    assert "notes_search" in names
    assert "notes_update" in names
    assert "notes_append" in names
    assert "notes_delete" in names
    assert "notes_count" in names
    ga = next(t for t in LIVE_BACKEND_TOOLS if t["name"] == "google_account")
    assert "grant_notes" in ga["parameters"]["properties"]["action"]["enum"]


def test_notes_tools_offloaded():
    ex = ToolExecutor(
        calendar=CalendarToolWrappers(lambda **kw: AgentResult("success", "x")),
        gmail=GmailToolWrappers(lambda **kw: AgentResult("success", "x")),
        notes=NotesToolWrappers(lambda **kw: AgentResult("success", "x")),
    )
    assert ex.is_offloaded("notes_add")
    assert ex.is_offloaded("notes_update")
    assert ex.is_offloaded("notes_delete")
    assert ex.is_offloaded("notes_count")


def test_realtime_notes_action_enum_includes_mutations():
    from assistant import TOOLS

    notes_tool = next(t for t in TOOLS if t["name"] == "notes_action")
    actions = notes_tool["parameters"]["properties"]["action"]["enum"]
    for name in ("add", "read", "search", "count", "update", "append", "delete"):
        assert name in actions
