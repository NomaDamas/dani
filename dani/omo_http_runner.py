from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from subprocess import TimeoutExpired
from uuid import uuid4

from dani.errors import (
    check_opencode_session_missing_error,
    check_transient_capacity_error,
)
from dani.models import DEFAULT_AGENT_TIMEOUT_SECONDS, JobRecord, SessionRecord
from dani.opencode_http import (
    OPENCODE_BIN_DEFAULT,
    PERMISSION_RESPONSE_DEFAULT,
    HttpTaskHandle,
    OpencodeClient,
    OpencodeEventConsumer,
    OpencodeHttpError,
    OpencodeServerManager,
)

logger = logging.getLogger(__name__)

ULTRAWORK_PROMPT_PREFIX = "ultrawork\n\n"
COMMENT_ONLY_STAGES = frozenset({
    "issue_request",
    "issue_followup",
    "issue_request_recovery",
    "issue_followup_recovery",
})
DANI_OPENCODE_SERVER_URL_ENV = "DANI_OPENCODE_SERVER_URL"
DANI_OPENCODE_PERMISSION_RESPONSE_ENV = "DANI_OPENCODE_PERMISSION_RESPONSE"
SESSION_ID_PATTERN = re.compile(r"^ses_[A-Za-z0-9]+$")

ClientFactory = Callable[[str], OpencodeClient]
ConsumerFactory = Callable[[OpencodeClient, Path, str], OpencodeEventConsumer]


class OmoHttpRunner:
    def __init__(
        self,
        run_dir: Path,
        *,
        opencode_bin: str = OPENCODE_BIN_DEFAULT,
        server_manager: OpencodeServerManager | None = None,
        client_factory: ClientFactory | None = None,
        consumer_factory: ConsumerFactory | None = None,
        permission_response: str | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.opencode_bin = opencode_bin
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tasks: dict[str, HttpTaskHandle] = {}
        self._session_to_base_url: dict[str, str] = {}
        self._handle_to_session: dict[str, str] = {}
        self._consumers: dict[str, OpencodeEventConsumer] = {}
        self._clients: dict[str, OpencodeClient] = {}
        self._permission_response = self._resolve_permission_response(permission_response)
        self._server_manager = server_manager or OpencodeServerManager(
            run_dir,
            opencode_bin=opencode_bin,
            external_server_url=os.environ.get(DANI_OPENCODE_SERVER_URL_ENV),
        )
        self._client_factory = client_factory or _default_client_factory
        self._consumer_factory = consumer_factory or _default_consumer_factory

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord:
        prompt_text = self._decorated_prompt(prompt, stage=job.stage)
        session_dir, prompt_path, request_log_path, event_log_path, handle = self._prepare_session_files(job)
        prompt_path.write_text(prompt_text, encoding="utf-8")
        client, consumer = self._get_client_and_consumer(repo_path, event_log_path)
        try:
            session = client.create_session(
                directory=str(repo_path),
                title=self._session_title(job),
            )
        except Exception:
            logger.exception("opencode create_session failed for repo=%s job=%s", repo_path, job.id)
            raise
        self._submit_prompt_and_register(
            client=client,
            consumer=consumer,
            session_id=session.id,
            directory=session.directory,
            prompt_text=prompt_text,
            handle=handle,
            request_log_path=request_log_path,
            event_log_path=event_log_path,
        )
        del session_dir
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            runtime_handle=handle,
            prompt_path=str(prompt_path),
            script_path=str(request_log_path),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            codex_session_id=session.id,
            stdout_path=str(event_log_path),
            stderr_path=str(event_log_path),
        )

    def resume(self, repo_path: Path, job: JobRecord, prompt: str, codex_session_id: str) -> SessionRecord:
        if not codex_session_id:
            msg = "codex_session_id is required to resume an opencode session"
            raise ValueError(msg)
        prompt_text = self._decorated_prompt(prompt, stage=job.stage)
        session_dir, prompt_path, request_log_path, event_log_path, handle = self._prepare_session_files(job)
        prompt_path.write_text(prompt_text, encoding="utf-8")
        client, consumer = self._get_client_and_consumer(repo_path, event_log_path)
        try:
            client.get_session(codex_session_id, directory=str(repo_path))
        except OpencodeHttpError as exc:
            if self._is_session_missing_response(exc):
                check_opencode_session_missing_error(f"Session not found: {codex_session_id}\n{exc.body}")
                msg = f"opencode session {codex_session_id} not found (status {exc.status})"
                raise OpencodeHttpError(status=exc.status, body=msg, url=exc.url) from None
            raise
        self._submit_prompt_and_register(
            client=client,
            consumer=consumer,
            session_id=codex_session_id,
            directory=str(repo_path),
            prompt_text=prompt_text,
            handle=handle,
            request_log_path=request_log_path,
            event_log_path=event_log_path,
        )
        del session_dir
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            runtime_handle=handle,
            prompt_path=str(prompt_path),
            script_path=str(request_log_path),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            codex_session_id=codex_session_id,
            stdout_path=str(event_log_path),
            stderr_path=str(event_log_path),
        )

    def wait(
        self, runtime_handle: str, *, poll_interval: float = 0.5, timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS
    ) -> None:
        del poll_interval
        with self._lock:
            task = self._tasks.get(runtime_handle)
        if task is None:
            return
        try:
            task.wait(timeout=timeout_seconds)
        except TimeoutExpired as exc:
            msg = f"opencode session did not reach idle/error before timeout: {runtime_handle}"
            raise TimeoutError(msg) from exc
        outcome = task.outcome()
        if outcome.aborted:
            return
        if outcome.error_message:
            check_opencode_session_missing_error(outcome.error_message)
            check_transient_capacity_error(outcome.error_message)
            raise RuntimeError(outcome.error_message)

    def close_session(self, runtime_handle: str) -> None:
        with self._lock:
            task = self._tasks.pop(runtime_handle, None)
            session_id = self._handle_to_session.pop(runtime_handle, None)
        if task is None:
            return
        try:
            task.terminate()
        finally:
            if session_id is not None:
                consumer = self._consumer_for_session(session_id)
                if consumer is not None:
                    consumer.unregister_session(session_id)

    def get_session_id(self, runtime_handle: str) -> str | None:
        with self._lock:
            session_id = self._handle_to_session.get(runtime_handle)
        if session_id and SESSION_ID_PATTERN.match(session_id):
            return session_id
        return None

    def can_resume(self, session_id: str) -> bool:
        return bool(session_id) and session_id.startswith("ses_")

    def shutdown(self) -> None:
        with self._lock:
            consumers = list(self._consumers.values())
            self._consumers.clear()
            self._clients.clear()
            self._tasks.clear()
            self._handle_to_session.clear()
            self._session_to_base_url.clear()
        for consumer in consumers:
            consumer.stop()
        self._server_manager.shutdown_all()

    def _decorated_prompt(self, prompt: str, *, stage: str | None = None) -> str:
        if prompt.lstrip().lower().startswith("ultrawork"):
            return prompt
        if stage in COMMENT_ONLY_STAGES:
            return prompt
        return f"{ULTRAWORK_PROMPT_PREFIX}{prompt}"

    def _session_title(self, job: JobRecord) -> str:
        suffix = job.id[:8] if job.id else "anon"
        if job.issue_number:
            return f"dani:{job.stage}:#{job.issue_number}:{suffix}"
        if job.pr_number:
            return f"dani:{job.stage}:pr#{job.pr_number}:{suffix}"
        return f"dani:{job.stage}:{suffix}"

    def _prepare_session_files(self, job: JobRecord) -> tuple[Path, Path, Path, Path, str]:
        session_token = uuid4().hex[:10]
        handle = f"dani-{job.stage}-{session_token}"
        session_dir = self.run_dir / handle
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        request_log_path = session_dir / "request.json"
        event_log_path = session_dir / "events.jsonl"
        return session_dir, prompt_path, request_log_path, event_log_path, handle

    def _get_client_and_consumer(
        self, repo_path: Path, event_log_path: Path
    ) -> tuple[OpencodeClient, OpencodeEventConsumer]:
        base_url = self._server_manager.get_server_for_repo(repo_path)
        with self._lock:
            client = self._clients.get(base_url)
            if client is None:
                client = self._client_factory(base_url)
                self._clients[base_url] = client
            consumer = self._consumers.get(base_url)
            if consumer is None:
                consumer = self._consumer_factory(client, event_log_path, self._permission_response)
                consumer.start()
                self._consumers[base_url] = consumer
        return client, consumer

    def _consumer_for_session(self, session_id: str) -> OpencodeEventConsumer | None:
        with self._lock:
            base_url = self._session_to_base_url.get(session_id)
            if base_url is None:
                return None
            return self._consumers.get(base_url)

    def _submit_prompt_and_register(
        self,
        *,
        client: OpencodeClient,
        consumer: OpencodeEventConsumer,
        session_id: str,
        directory: str,
        prompt_text: str,
        handle: str,
        request_log_path: Path,
        event_log_path: Path,
    ) -> HttpTaskHandle:
        state = consumer.register_session(session_id, directory=directory, event_log_path=event_log_path)
        try:
            client.send_prompt_async(
                session_id,
                prompt_text=prompt_text,
                directory=directory,
            )
        except Exception:
            consumer.unregister_session(session_id)
            raise
        try:
            self._write_request_log(request_log_path, session_id=session_id, prompt_text=prompt_text)
        except OSError:
            logger.debug("failed to write request log", exc_info=True)
        task_handle = HttpTaskHandle(
            session_id=session_id,
            directory=directory,
            client=client,
            consumer=consumer,
            state=state,
        )
        with self._lock:
            self._tasks[handle] = task_handle
            self._handle_to_session[handle] = session_id
            self._session_to_base_url[session_id] = client.base_url
        return task_handle

    @staticmethod
    def _is_session_missing_response(exc: OpencodeHttpError) -> bool:
        if exc.status == 404:
            return True
        if exc.status == 400:
            body_lower = exc.body.lower() if exc.body else ""
            if "must start with" in body_lower and "ses" in body_lower:
                return True
            if "invalid_format" in body_lower and "sessionid" in body_lower:
                return True
        return False

    @staticmethod
    def _resolve_permission_response(explicit: str | None) -> str:
        if explicit and explicit in {"once", "always", "reject"}:
            return explicit
        env_value = os.environ.get(DANI_OPENCODE_PERMISSION_RESPONSE_ENV, "").strip().lower()
        if env_value in {"once", "always", "reject"}:
            return env_value
        return PERMISSION_RESPONSE_DEFAULT

    @staticmethod
    def _write_request_log(path: Path, *, session_id: str, prompt_text: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "prompt_text": prompt_text,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def _default_client_factory(base_url: str) -> OpencodeClient:
    password = os.environ.get("OPENCODE_SERVER_PASSWORD") or None
    return OpencodeClient(base_url, password=password)


def _default_consumer_factory(
    client: OpencodeClient,
    event_log_path: Path,
    permission_response: str,
) -> OpencodeEventConsumer:
    return OpencodeEventConsumer(
        client,
        event_log_path=event_log_path,
        permission_response=permission_response,
    )
