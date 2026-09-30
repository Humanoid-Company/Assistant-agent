# GPT-Live Migration Report

## Summary

Migrated the voice layer to support **GPT-Live (`gpt-live-1`)** with **Responses delegation** as a parallel engine, while keeping the existing **OpenAI Realtime** implementation as a configurable legacy fallback.

Calendar business logic (`CalendarAgent`, `PendingStore`, Google Calendar integration, confirmation/`op_id` atomicity) remains authoritative in Python. Gmail is intentionally **not** exposed on the Live backend tool set.

Default engine remains **`VOICE_ENGINE=realtime`** (conservative). Opt in with `VOICE_ENGINE=live`.

OpenAI Python SDK upgraded: `3.3.0` → `3.22.1` (adds `client.live`).

## New architecture

```text
microphone (sleep: 16 kHz STT wake)
    |
    v  wake
GPT-Live-1  (conversation / full duplex / interruptions)
    |
    | Responses delegation (parallel_tool_calls=false)
    v
backend model (OPENAI_LIVE_BACKEND_MODEL, default gpt-6-luna)
    |
    v
ToolExecutor
    |
    +-- CalendarToolWrappers --> CalendarAgent --> PendingStore --> Google Calendar
    +-- session tools (name/voice/language/end/check_connection/robot/google_account)
    |
    (Gmail tools NOT registered on Live)
```

Legacy path unchanged in behavior:

```text
Assistant --> RealtimeConversation (realtime_client.py) --> same router tools including Gmail
```

## Files changed / added

| File | Change | Why |
|------|--------|-----|
| `pyproject.toml` / `uv.lock` | openai>=3.12 / 3.22.1 | `client.live` support |
| `config.py` | `VOICE_ENGINE`, Live model/voice/rate settings | Engine + Live config |
| `.env.example` | Documented new vars | Operator clarity |
| `assistant.py` | Engine selector; Live awake path; ToolExecutor wiring; Realtime path preserved | Orchestration only for Live; legacy intact |
| `prompts/live_prompt.py` | Short LIVE_PROMPT | Voice style / delegation / safety |
| `prompts/backend_prompt.py` | BACKEND_PROMPT | Calendar rules / confirmation |
| `tools/executor.py` | Central ToolExecutor | Move tool dispatch out of assistant for Live |
| `tools/calendar_tools.py` | Thin wrappers | No CalendarAgent rewrite |
| `tools/live_schemas.py` | Live backend tool schemas | No Gmail / note_emotion / dispatch_task |
| `tools/results.py` | Structured ToolResult | Machine-readable backend truth |
| `tools/task_context.py` | TaskRevisionTracker | Stale delegated work protection |
| `voice/base.py` | VoiceSession Protocol | Engine abstraction |
| `voice/live_session.py` | GPT-Live session | Continuous audio, nested response.event, function_call_output + response.create continuation |
| `voice/playback.py` | PlaybackTracker | Local queue / interrupt independent of backend |
| `voice/mic.py` | LiveMicCapture | Prefer native 24 kHz; 16k→24k fallback |
| `voice/delegation.py` | Event parse helpers | Testable nested event handling |
| `voice/realtime_legacy.py` | Adapter | Wrap Realtime without rewriting it |
| `voice/factory.py` | Engine factory | Selection by config |
| `tests/test_voice_engine_live.py` | New unit tests | Engine, executor, delegation, stale, playback, continuation |
| `MANUAL_LIVE_TESTS.md` | Manual checklist | Regression scenarios |
| `README.md` | Live docs | How to run both engines |
| `GPT_LIVE_MIGRATION_REPORT.md` | This file | Migration record |

Unchanged on purpose: `realtime_client.py` workarounds, `agents/calendar_agent.py`, `agents/pending_store.py`, `agents/gmail_agent.py`, Gmail OAuth.

## Legacy compatibility

```bash
# Legacy Realtime (default) — Calendar + Gmail
VOICE_ENGINE=realtime
uv run python main.py

# GPT-Live — Calendar via Responses delegation (no Gmail tools)
VOICE_ENGINE=live
uv run python main.py
```

PowerShell:

```powershell
$env:VOICE_ENGINE="live"; uv run python main.py
$env:VOICE_ENGINE="realtime"; uv run python main.py
```

## Environment / config

| Variable | Default | Notes |
|----------|---------|-------|
| `VOICE_ENGINE` | `realtime` | `live` or `realtime` |
| `OPENAI_LIVE_MODEL` | `gpt-live-1` | Voice model |
| `OPENAI_LIVE_BACKEND_MODEL` | `gpt-6-luna` | Responses delegation backend |
| `OPENAI_LIVE_VOICE` | `marin` | Live output voice |
| `OPENAI_LIVE_AUDIO_RATE` | `24000` | PCM16 mono |

Also requires existing `OPENAI_API_KEY` and Google OAuth files (unchanged).

## Decisions documented

1. **Default engine = realtime** — reversible migration; Live is opt-in.
2. **`parallel_tool_calls=false`** for Live Responses — safer for calendar mutations.
3. **Gmail out of Live tool set** — Realtime Gmail path untouched.
4. **`note_emotion` / `dispatch_task`** not registered on Live — legacy Realtime keeps them.
5. **Voice change on Live** restarts the Live session only (process stays up). Realtime still requires process restart.
6. **Native 24 kHz mic** when PortAudio allows; else documented 16→24 resample fallback. Sleep STT stays 16 kHz.
7. **TaskRevisionTracker** wraps PendingStore; does not replace `op_id` atomicity.
8. **Live `response.create`** only continues delegated Responses after `function_call_output` — never used as a manual “user stopped talking” voice-turn trigger.

### `response.create` classification

| Location | Class |
|----------|-------|
| `realtime_client.py` (multiple) | **Legacy Realtime voice-turn / follow-up control** |
| `voice/live_session.py` (`connection.response.create`) | **Valid GPT-Live Responses delegation continuation** |
| Tests mirroring the above | Matching the class under test |

## Tests

Command: `uv run python -m pytest -q` (also `.venv\Scripts\python.exe -m pytest -q`)

| Result | Count |
|--------|-------|
| Passed | **171** |
| Failed | 0 |
| Skipped | 0 |

New coverage includes: engine selection, ToolExecutor routing/errors, stale revision, nested `response.event` → `output_item.done` only, function_call_output + continue once, transcript fragments, playback interrupt (mocked), Live schemas exclude Gmail/`note_emotion`/`dispatch_task`.

Hardware-dependent Live E2E against OpenAI is covered by `MANUAL_LIVE_TESTS.md` (not automated here).

## Known limitations

- **Gmail not migrated to Live** (intentional).
- **Robot**: local emergency/fast phrases still match on Live transcripts; full robot redesign deferred. Realtime robot trigger via session STT preserved.
- **PendingStore** still in-memory (SQLite deferred).
- **Live history inject** across sleep/wake is lighter than Realtime `inject_history` (transcript turns retained in Assistant memory; no full Realtime-style item inject yet).
- Native 24 kHz capture may fall back to resample on some devices.
- Backend model default `gpt-6-luna` must be available on the API project; override via `OPENAI_LIVE_BACKEND_MODEL` if needed.

## Old Realtime code intentionally retained

`realtime_client.py` keeps deferred `response.create`, speech_started/stopped handling, silent-tool follow-up suppression, tool_choice queues, and related barge-in/response-overlap workarounds. They remain required for `VOICE_ENGINE=realtime` regression protection and were **not** ported into the Live client.

## Recommended next phase (not implemented)

1. Gmail/API modernization.
2. Gmail structured tools on Live backend.
3. Durable SQLite PendingStore.
4. Stronger create/update Google reconciliation.
5. Remove Realtime after Live regression sign-off.
6. Richer Live history restore across wake cycles.

## Credential hygiene

- `.gitignore` already covers `.env`, `credentials/*`, `client_secret*.json`, `data/*`.
- No secrets added to configs/tests/report.
- Tests mock Google; no live OAuth required for unit suite.
