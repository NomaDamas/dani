from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dani.opencode_http.client import OpencodeClient
    from dani.opencode_http.event_consumer import CompletionState, OpencodeEventConsumer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HttpTaskOutcome:
    completed: bool
    aborted: bool
    error_message: str | None
    error_payload: dict | None


class HttpTaskHandle:
    def __init__(
        self,
        *,
        session_id: str,
        directory: str,
        client: OpencodeClient,
        consumer: OpencodeEventConsumer,
        state: CompletionState,
    ) -> None:
        self.session_id = session_id
        self.directory = directory
        self._client = client
        self._consumer = consumer
        self._state = state
        self._closed = threading.Event()
        self._exit_code: int | None = None

    @property
    def state(self) -> CompletionState:
        return self._state

    def poll(self) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        if not self._state.event.is_set():
            return None
        self._exit_code = 0 if self._state.error_message is None and not self._state.aborted else 1
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        deadline_seconds = float(timeout) if timeout is not None else None
        signaled = self._state.event.wait(timeout=deadline_seconds)
        if not signaled:
            from subprocess import TimeoutExpired

            raise TimeoutExpired(cmd=f"opencode-session:{self.session_id}", timeout=deadline_seconds or 0.0)
        self._exit_code = 0 if self._state.error_message is None and not self._state.aborted else 1
        return self._exit_code

    def terminate(self) -> None:
        self._abort()

    def kill(self) -> None:
        self._abort()

    def _abort(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._client.abort_session(self.session_id, directory=self.directory)
        except Exception:
            logger.debug("abort failed for session %s", self.session_id, exc_info=True)
        self._consumer.mark_aborted(self.session_id)
        if self._exit_code is None:
            self._exit_code = 1

    def outcome(self) -> HttpTaskOutcome:
        return HttpTaskOutcome(
            completed=self._state.event.is_set(),
            aborted=self._state.aborted,
            error_message=self._state.error_message,
            error_payload=self._state.error_payload,
        )

    def wait_until_settled(self, *, deadline_seconds: float, poll_interval: float = 0.1) -> bool:
        end = time.monotonic() + deadline_seconds
        while time.monotonic() < end:
            if self._state.event.is_set():
                return True
            time.sleep(poll_interval)
        return self._state.event.is_set()
