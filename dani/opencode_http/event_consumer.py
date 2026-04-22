from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dani.opencode_http.client import OpencodeClient, OpencodeHttpError

logger = logging.getLogger(__name__)

PERMISSION_RESPONSE_DEFAULT = "once"
SSE_RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)
STATUS_FALLBACK_POLL_SECONDS = 5.0


@dataclass(slots=True)
class CompletionState:
    sessionID: str
    event: threading.Event = field(default_factory=threading.Event)
    error_payload: dict[str, Any] | None = None
    error_message: str | None = None
    aborted: bool = False
    status: str = "submitted"


class OpencodeEventConsumer:
    def __init__(
        self,
        client: OpencodeClient,
        *,
        event_log_path: Path | None = None,
        permission_response: str = PERMISSION_RESPONSE_DEFAULT,
    ) -> None:
        self._client = client
        self._default_event_log_path = event_log_path
        self._permission_response = permission_response
        self._sessions: dict[str, CompletionState] = {}
        self._session_log_paths: dict[str, Path] = {}
        self._session_directories: dict[str, str] = {}
        self._directory_threads: dict[str, threading.Thread] = {}
        self._directory_connected: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._fallback_thread: threading.Thread | None = None
        self._started = False
        if event_log_path is not None:
            event_log_path.parent.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._stop_event.clear()
            self._started = True
        self._fallback_thread = threading.Thread(
            target=self._run_status_fallback_loop,
            name="opencode-status-fallback",
            daemon=True,
        )
        self._fallback_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            threads = list(self._directory_threads.values())
        for thread in threads:
            thread.join(timeout=2.0)
        fallback = self._fallback_thread
        if fallback is not None:
            fallback.join(timeout=2.0)

    def register_session(
        self,
        session_id: str,
        *,
        directory: str | None = None,
        event_log_path: Path | None = None,
    ) -> CompletionState:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                state = CompletionState(sessionID=session_id)
                self._sessions[session_id] = state
            if event_log_path is not None:
                event_log_path.parent.mkdir(parents=True, exist_ok=True)
                self._session_log_paths[session_id] = event_log_path
            if directory:
                self._session_directories[session_id] = directory
        if directory:
            self._ensure_directory_listener(directory)
        return state

    def unregister_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._session_log_paths.pop(session_id, None)
            self._session_directories.pop(session_id, None)

    def get_state(self, session_id: str) -> CompletionState | None:
        with self._lock:
            return self._sessions.get(session_id)

    def mark_aborted(self, session_id: str) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return
            state.aborted = True
            state.status = "aborted"
            state.event.set()

    def _ensure_directory_listener(self, directory: str) -> None:
        with self._lock:
            existing = self._directory_threads.get(directory)
            if existing is not None and existing.is_alive():
                return
            connected = threading.Event()
            self._directory_connected[directory] = connected
            thread = threading.Thread(
                target=self._run_event_loop_for_directory,
                args=(directory, connected),
                name=f"opencode-event-{Path(directory).name or 'root'}",
                daemon=True,
            )
            self._directory_threads[directory] = thread
        thread.start()

    def _run_event_loop_for_directory(self, directory: str, connected: threading.Event) -> None:
        backoff_index = 0
        while not self._stop_event.is_set():
            try:
                for event in self._client.stream_events(directory=directory, stop_event=self._stop_event):
                    connected.set()
                    backoff_index = 0
                    self._handle_event(event)
                    if self._stop_event.is_set():
                        break
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                connected.clear()
                backoff = SSE_RECONNECT_BACKOFF_SECONDS[min(backoff_index, len(SSE_RECONNECT_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "opencode SSE stream error for directory=%s (%s); reconnecting in %.1fs",
                    directory,
                    type(exc).__name__,
                    backoff,
                )
                backoff_index += 1
                if self._stop_event.wait(timeout=backoff):
                    return
                continue
            connected.clear()
            if self._stop_event.is_set():
                return
            if self._stop_event.wait(timeout=1.0):
                return

    def _run_status_fallback_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=STATUS_FALLBACK_POLL_SECONDS):
                return
            self._poll_status_for_pending_sessions()

    def _poll_status_for_pending_sessions(self) -> None:
        with self._lock:
            pending: list[tuple[str, str | None]] = [
                (sid, self._session_directories.get(sid))
                for sid, state in self._sessions.items()
                if not state.event.is_set()
            ]
            all_connected = bool(self._directory_connected) and all(
                ev.is_set() for ev in self._directory_connected.values()
            )
        if all_connected or not pending:
            return
        directories_to_probe: set[str | None] = {directory for _sid, directory in pending}
        for directory in directories_to_probe:
            try:
                statuses = self._client.session_status(directory=directory)
            except Exception as exc:
                logger.debug("session_status fallback poll failed for %s: %s", directory, exc)
                continue
            self._mark_idle_for_matching(pending, directory, statuses)

    def _mark_idle_for_matching(
        self,
        pending: list[tuple[str, str | None]],
        directory: str | None,
        statuses: dict[str, dict[str, Any]],
    ) -> None:
        for session_id, session_dir in pending:
            if session_dir != directory:
                continue
            status = statuses.get(session_id)
            if not isinstance(status, dict):
                continue
            if str(status.get("type", "")) == "idle":
                self._mark_idle(session_id)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        properties = event.get("properties") or {}
        if not isinstance(properties, dict):
            properties = {}
        self._log_event(event_type, properties)
        if event_type == "session.idle":
            self._mark_idle(str(properties.get("sessionID", "")))
        elif event_type == "session.error":
            self._mark_error(properties)
        elif event_type == "permission.updated":
            self._auto_grant_permission(properties)
        elif event_type == "session.status":
            session_id = str(properties.get("sessionID", ""))
            status = properties.get("status") or {}
            status_type = str(status.get("type", "")) if isinstance(status, dict) else ""
            with self._lock:
                state = self._sessions.get(session_id)
                if state is not None and status_type:
                    state.status = status_type

    def _mark_idle(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state.event.is_set():
                return
            state.status = "idle"
            state.event.set()

    def _mark_error(self, properties: dict[str, Any]) -> None:
        session_id = str(properties.get("sessionID", "") or "")
        error_payload = properties.get("error") or {}
        if not isinstance(error_payload, dict):
            error_payload = {"raw": error_payload}
        message = ""
        nested = error_payload.get("data") if isinstance(error_payload, dict) else None
        if isinstance(nested, dict):
            message = str(nested.get("message", "") or "")
        if not message:
            message = str(error_payload.get("name", "") or error_payload.get("type", "") or "session.error")
        if not session_id:
            with self._lock:
                pending = [sid for sid, state in self._sessions.items() if not state.event.is_set()]
            for sid in pending:
                self._record_error_locked(sid, error_payload, message)
            return
        self._record_error_locked(session_id, error_payload, message)

    def _record_error_locked(self, session_id: str, error_payload: dict[str, Any], message: str) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state.event.is_set():
                return
            state.error_payload = error_payload
            state.error_message = message
            state.status = "error"
            state.event.set()

    def _auto_grant_permission(self, properties: dict[str, Any]) -> None:
        session_id = str(properties.get("sessionID", "") or "")
        permission_id = str(properties.get("id", "") or "")
        if not session_id or not permission_id:
            return
        with self._lock:
            directory = self._session_directories.get(session_id)
        try:
            self._client.respond_permission(
                session_id,
                permission_id,
                response=self._permission_response,
                directory=directory,
            )
        except OpencodeHttpError as exc:
            logger.warning(
                "permission grant rejected (status=%s) for session=%s permission=%s: %s",
                exc.status,
                session_id,
                permission_id,
                exc.body[:200],
            )
        except Exception:
            logger.warning(
                "permission grant failed for session=%s permission=%s",
                session_id,
                permission_id,
                exc_info=True,
            )

    def _log_event(self, event_type: str, properties: dict[str, Any]) -> None:
        target_paths = self._resolve_log_paths(properties)
        if not target_paths:
            return
        line = (
            json.dumps({
                "ts": time.time(),
                "type": event_type,
                "properties": properties,
            })
            + "\n"
        )
        for path in target_paths:
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                logger.debug("failed to write opencode event log to %s", path, exc_info=True)

    def _resolve_log_paths(self, properties: dict[str, Any]) -> list[Path]:
        candidates: list[Path] = []
        session_id = self._extract_session_id(properties)
        with self._lock:
            session_log = self._session_log_paths.get(session_id) if session_id else None
            default_log = self._default_event_log_path
        if session_log is not None:
            candidates.append(session_log)
        if default_log is not None and default_log != session_log:
            candidates.append(default_log)
        return candidates

    @staticmethod
    def _extract_session_id(properties: dict[str, Any]) -> str | None:
        sid = properties.get("sessionID")
        if isinstance(sid, str) and sid:
            return sid
        info = properties.get("info")
        if isinstance(info, dict):
            inner = info.get("sessionID") or info.get("id")
            if isinstance(inner, str) and inner:
                return inner
        part = properties.get("part")
        if isinstance(part, dict):
            inner = part.get("sessionID")
            if isinstance(inner, str) and inner:
                return inner
        return None
