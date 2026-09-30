"""Manual regression checklist for GPT-Live Calendar migration.

Run with VOICE_ENGINE=live after `uv sync`.

## Normal conversation
- [ ] User speaks casually; assistant responds without tools.

## Calendar create
- [ ] User: create meeting tomorrow 15:00 → confirmation asked.
- [ ] User: yes → event created exactly once; success only after tool result.

## Calendar delete
- [ ] User: delete tomorrow's meeting → identify → confirm → deleted once.

## Reject
- [ ] Pending delete → user says no → no Google mutation.

## Correction before confirmation
- [ ] Prepare Friday → user says Monday → old op stale/rejected → only latest confirmable.

## Interruption while speaking
- [ ] Local playback stops/drains; mic continues; conversation usable.
- [ ] Backend work is NOT assumed cancelled solely because playback stopped.

## Interruption while backend works
- [ ] User changes request mid-flight → stale revision cannot mutate incorrectly.

## Tool error
- [ ] Simulated Google error → assistant does not claim success.

## Ambiguous event
- [ ] Multiple matches → clarification asked.

## Sleep/wake
- [ ] Wake starts one Live session; sleep closes cleanly; wake again is a fresh session.

## Engine switch
- [ ] VOICE_ENGINE=realtime → legacy Realtime + Gmail tools still work.
- [ ] VOICE_ENGINE=live → no Gmail tools; calendar confirmations work.

## Voice change (Live)
- [ ] change_voice restarts Live session only — process stays running.
"""
