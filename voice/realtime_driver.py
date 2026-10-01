"""Assistant mixin: drives an awake session on the legacy Realtime engine."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from config import (
    REALTIME_VOICE,
)
from realtime_client import RealtimeConversation
from robot_control import ROBOT_ACTIONS
from robot_triggers import _ROBOT_ACTION_TEXT, _match_robot_trigger
from tools.realtime_schemas import _NO_FOLLOWUP_TOOLS, _SILENT_TOOLS, TOOLS
from tools.router_bridge import (
    _describe_connection_status,
    _gmail_kwargs,
    _model_tool_output,
    _notes_kwargs,
    _parse_router_reply,
    _router_tool_failure_message,
    _run_connectivity_checks,
    _should_speak_router_result,
    calendar_tool_args,
)
from voice.base import State
from voice.options import _sanitize_name

logger = logging.getLogger(__name__)


class RealtimeDriverMixin:
    # Set up by Assistant.__init__ / the awake-session loop.
    rt: RealtimeConversation | None
    _running: bool
    _history: list[dict]
    _history_cutoff: int | None
    _pending_cutoff: int

    def _active_rt(self) -> RealtimeConversation:
        """The open Realtime session — tool handlers and turn checks only run inside one."""
        if self.rt is None:
            raise RuntimeError("Realtime handler called outside an awake session")
        return self.rt
    def _run_awake_session_realtime(self) -> None:
        """Legacy Realtime path — preserved with existing workarounds."""
        self._sleep_requested = False
        self._voice_change_pending = False
        # Where in this session's turns the goodbye exchange starts — trimmed
        # off before carrying history into the next wake, so a restored
        # session never opens with a farewell already in context (that was
        # making the model call end_conversation immediately instead of just
        # greeting the user).
        self._history_cutoff: int | None = None
        self._pending_cutoff = len(self._history)
        # One id per awake session — keeps dispatch_task's multi-turn calendar
        # confirmation ("так"/"ні") tied to the same session.
        self._router_session_id = str(uuid.uuid4())
        self.rt = RealtimeConversation(
            tools=TOOLS,
            on_tool_call=self._handle_tool_call,
            silent_tools=_SILENT_TOOLS,
            no_followup_tools=_NO_FOLLOWUP_TOOLS,
            voice=self._memory.get("realtime_voice", REALTIME_VOICE),
        )
        try:
            self.rt.connect(self._build_instructions(), mic_read_chunk=self.stt.read_chunk)
            if self._history:
                self.rt.inject_history(self._history)
            self.rt.say("Слухаю!")
            if alert := self._connectivity_watcher.pop_alert():
                self.rt.wait_until_response_done()
                self.rt.say(alert)
            if pending := self._pop_deferred_announcement():
                self.rt.wait_until_response_done()
                self.rt.say(pending)

            t_start = time.monotonic()
            while self._running and not self._sleep_requested:
                pcm = self.rt.pump(timeout=0.05)
                if pcm:
                    self._pending_cutoff = len(self.rt.get_turns())
                    # Speculative: the model starts on the reply now, in parallel with the
                    # input transcription (~0.3–1.5 s) that the robot fast-path needs.
                    # A matched robot command takes the turn back via cancel_reply().
                    self._model_robot_action_this_turn = False
                    self.rt.create_response()
                    action = self._check_robot_trigger()
                    if action and not self._model_robot_action_this_turn:
                        self.rt.cancel_reply()
                        self._execute_robot_trigger(action)

            logger.info("[latency] Awake session duration: %.1fs", time.monotonic() - t_start)

            if self._sleep_requested:
                # Let the response that called end_conversation/change_voice
                # finish first — otherwise say() below fires while it's still
                # streaming, producing two overlapping voices. Then drop
                # whatever's left in the playback queue so only our own
                # farewell is heard. Skipped for a pending voice change — the
                # model already announced the restart itself in its own
                # natural reply; a scripted "До побачення!" on top would be
                # a redundant second goodbye.
                self.rt.wait_until_response_done()
                self.rt.player.stop()
                if not self._voice_change_pending:
                    self.rt.say("До побачення!")
                    self.rt.wait_until_response_done()
        except Exception:
            # Whatever broke, the mic feeder/reader threads on self.rt MUST be
            # torn down before we go back to SLEEPING — otherwise they keep
            # reading from the same persistent mic stream as the wake-phrase
            # loop, splitting audio between two readers and making the
            # assistant seem to "stop hearing" the user entirely.
            logger.error("Awake session crashed, closing Realtime session before sleeping.", exc_info=True)
            raise
        finally:
            turns = self.rt.get_turns()
            self._history = turns[: self._history_cutoff] if self._history_cutoff is not None else turns
            self.rt.close()
            self.rt = None
            if self._voice_change_pending:
                logger.info("Voice changed — stopping so the new voice takes effect on next run.")
                self._running = False
            else:
                self.state = State.SLEEPING
                time.sleep(1.5)  # let echo of the farewell settle before listening again

    def _handle_tool_call(self, name: str, args: dict, call_id: str) -> str | None:
        """Return the tool's result text, or None if it's running in the
        background and will report back later via rt.submit_deferred_tool_result."""
        try:
            if name == "set_assistant_name":
                new_name = _sanitize_name(args.get("name", ""))
                if not new_name:
                    return "Не зрозумів нового імені."
                return self._set_assistant_name(new_name)
            if name == "change_voice":
                return self._change_voice(args.get("voice", "").strip().lower())
            if name == "change_language":
                return self._change_language(args.get("language", "").strip().lower())
            if name == "end_conversation":
                self._sleep_requested = True
                self._history_cutoff = self._pending_cutoff
                return "Розмову завершено."
            if name == "note_emotion":
                emotion = args.get("emotion", "").strip()
                logger.info("[emotion] %s", emotion)
                return ""
            if name == "control_robot":
                self._model_robot_action_this_turn = True
                self._handle_robot_action(call_id, args.get("action", "").strip().lower())
                return None
            if name == "google_account":
                self._run_router_tool(call_id, lambda: self._google_account(args))
                return None
            if name == "calendar_action":
                utterances = self._user_utterances()
                self._run_router_tool(
                    call_id,
                    lambda: self.router.calendar_action(
                        **calendar_tool_args(
                            args,
                            session_id=self._router_session_id,
                            user_utterances=utterances,
                        )
                    ),
                )
                return None
            if name == "gmail_action":
                gmail_args = _gmail_kwargs(args)
                gmail_args["session_id"] = self._router_session_id
                self._run_router_tool(call_id, lambda: self.router.gmail_action(**gmail_args))
                return None
            if name == "notes_action":
                notes_args = _notes_kwargs(args)
                notes_args["session_id"] = self._router_session_id
                self._run_router_tool(call_id, lambda: self.router.notes_action(**notes_args))
                return None
            if name == "web_search":
                self._run_web_search_realtime(call_id, args)
                return None
            if name == "dispatch_task":
                task = args.get("task", "").strip()
                if not task:
                    logger.warning("dispatch_task called with empty task (likely truncated by barge-in) — skipping.")
                    return "Не почув, що саме зробити — повтори, будь ласка."
                self._dispatch_task(call_id, task)
                return None
            if name == "check_connection":
                self._check_connection(call_id)
                return None
            logger.warning("Unknown tool call: %s", name)
            return f"Невідома команда: {name}"
        except Exception as exc:
            logger.error("Tool call %r failed: %s", name, exc, exc_info=True)
            return "Виникла помилка під час виконання команди."

    def _run_web_search_realtime(self, call_id: str, args: dict) -> None:
        """Realtime: return structured search JSON and let the model voice a short summary."""
        rt = self._active_rt()

        def worker() -> None:
            try:
                tr = self._web_search_tool_result(
                    args,
                    session_id=self._router_session_id,
                    delegation_id=None,
                )
                body = {
                    "status": tr.status,
                    "ok": tr.ok,
                    "message": tr.message,
                    "query": (tr.data or {}).get("query"),
                    "results": (tr.data or {}).get("results") or [],
                    "error": (tr.data or {}).get("error"),
                }
                output = json.dumps(body, ensure_ascii=False)
                rt.submit_deferred_tool_result(
                    call_id,
                    output,
                    trigger_followup=True,
                    allow_tool_calls=False,
                )
            except Exception as exc:
                logger.error("web_search failed: %s", type(exc).__name__, exc_info=True)
                rt.submit_deferred_tool_result(
                    call_id,
                    json.dumps(
                        {
                            "status": "error",
                            "ok": False,
                            "message": "Вебпошук тимчасово недоступний.",
                            "query": str(args.get("query") or ""),
                            "results": [],
                            "error": "web_search_unavailable",
                        },
                        ensure_ascii=False,
                    ),
                    trigger_followup=True,
                    allow_tool_calls=False,
                )

        threading.Thread(target=worker, daemon=True, name="web-search").start()

    def _user_utterances(self) -> list[str] | None:
        if self.rt is None:
            return None
        said = [
            turn["content"].strip()
            for turn in self.rt.get_turns()
            if turn.get("role") == "user" and isinstance(turn.get("content"), str) and turn["content"].strip()
        ]
        return said or None

    def _run_router_tool(self, call_id: str, fn) -> None:
        rt = self._active_rt()

        def worker() -> None:
            try:
                result = fn()
                _reply, awaiting = _parse_router_reply(result)
                logger.info(
                    "Router tool status=%s reason=%s op_id=%s pending_state=%s",
                    result.status,
                    (result.data or {}).get("reason_code"),
                    (result.data or {}).get("op_id"),
                    (result.data or {}).get("pending_state"),
                )
                output = _model_tool_output(result)
                if _should_speak_router_result(result):
                    # Deliver JSON for the model (op_id / status), then speak ourselves.
                    # Relying on a model follow-up fails when the next response is only
                    # note_emotion (silent) — observed: ambiguous cancel → silence.
                    rt.submit_deferred_tool_result(
                        call_id, output, trigger_followup=False, allow_tool_calls=False
                    )
                    try:
                        logger.info(
                            "Speaking router result status=%s via say() (fallback path)",
                            result.status,
                        )
                        rt.say(result.message)
                    except Exception:
                        logger.exception(
                            "say() failed for status=%s — falling back to model follow-up",
                            result.status,
                        )
                        rt.create_response(tool_choice="none")
                else:
                    rt.submit_deferred_tool_result(
                        call_id, output, allow_tool_calls=not awaiting
                    )
            except TypeError as exc:
                logger.error("Router tool rejected bad arguments: %s", type(exc).__name__)
                rt.submit_deferred_tool_result(call_id, _router_tool_failure_message(exc))
            except Exception as exc:
                logger.error("Router tool failed: %s", type(exc).__name__, exc_info=True)
                rt.submit_deferred_tool_result(call_id, _router_tool_failure_message(exc))

        threading.Thread(target=worker, daemon=True, name="router-tool").start()

    def _dispatch_task(self, call_id: str, task: str) -> None:
        """Run free-text through the local Agent Router on a background thread."""
        rt = self._active_rt()
        session_id = self._router_session_id

        def worker() -> None:
            try:
                result = self.router.handle_text(task, session_id=session_id)
                reply, awaiting = _parse_router_reply(result)
                logger.info("dispatch_task status=%s", result.status)
                rt.submit_deferred_tool_result(call_id, reply, allow_tool_calls=not awaiting)
            except Exception as exc:
                logger.error("Router dispatch failed: %s", exc, exc_info=True)
                rt.submit_deferred_tool_result(call_id, "Не вдалося виконати завдання.")

        threading.Thread(target=worker, daemon=True, name="router-dispatch").start()

    def _check_connection(self, call_id: str) -> None:
        rt = self._active_rt()

        def worker() -> None:
            result = _run_connectivity_checks(self.router)
            rt.submit_deferred_tool_result(call_id, _describe_connection_status(result))

        threading.Thread(target=worker, daemon=True, name="connection-check").start()

    def _check_robot_trigger(self) -> str | None:
        """Waits for this session's own input transcription of the utterance
        that just finished, checked against ROBOT_TRIGGER_PHRASES BEFORE the
        turn reaches the model. Uses the Realtime session's own transcript
        (gpt-4o-mini-transcribe) rather than a second, separate Google STT
        pass — running two different STT engines on the same audio let them
        disagree (e.g. session heard "Іде вперед", a parallel Google STT pass
        heard something else entirely), silently swallowing real matches."""
        text = self._active_rt().pump_for_transcript(timeout=1.5)
        if not text:
            logger.info("[robot-trigger] no transcript received (timeout/empty) — falling back to model")
            return None
        action = _match_robot_trigger(text.lower())
        if action:
            logger.info("[robot-trigger] %r -> %s (bypassing model)", text, action)
        else:
            logger.info("[robot-trigger] %r -> no match, falling back to model", text)
        return action

    def _execute_robot_trigger(self, action: str) -> None:
        """Runs a trigger-matched action directly and speaks a scripted
        confirmation via rt.say() — no tool call, no model involved, so
        there's no call_id to report back to (unlike _handle_robot_action)."""
        method = getattr(self.robot, action, None)
        if method is None:
            return
        try:
            method()
            self._active_rt().say(_ROBOT_ACTION_TEXT.get(action, "Готово."))
        except Exception as exc:
            logger.error("Robot trigger action %r failed: %s", action, exc, exc_info=True)
            self._active_rt().say("Не вдалося виконати команду роботом.")

    def _handle_robot_action(self, call_id: str, action: str) -> None:
        """Runs the physical action on a background thread — real hardware
        calls (movement) aren't instant, so this follows the same deferred-
        result pattern as _dispatch_task rather than blocking the live
        conversation."""
        rt = self._active_rt()
        method = getattr(self.robot, action, None) if action in ROBOT_ACTIONS else None

        def worker() -> None:
            if method is None:
                rt.submit_deferred_tool_result(call_id, f"Команда {action!r} не підтримується.")
                return
            try:
                method()
                rt.submit_deferred_tool_result(call_id, _ROBOT_ACTION_TEXT.get(action, "Готово."))
            except Exception as exc:
                logger.error("Robot action %r failed: %s", action, exc, exc_info=True)
                rt.submit_deferred_tool_result(call_id, "Не вдалося виконати команду роботом.")

        threading.Thread(target=worker, daemon=True, name="robot-action").start()
