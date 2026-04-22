from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dani.errors import RolloutMissingError, TransientCapacityError
from dani.models import JobRecord
from dani.omo_http_runner import OmoHttpRunner
from dani.opencode_http import (
    CompletionState,
    OpencodeClient,
    OpencodeEventConsumer,
    OpencodeHttpError,
    OpencodeServerError,
    OpencodeServerManager,
    OpencodeSessionInfo,
)


class FakeOpencodeClient:
    def __init__(self, base_url: str = "http://test-server") -> None:
        self.base_url = base_url
        self.created_sessions: list[dict[str, Any]] = []
        self.prompt_calls: list[dict[str, Any]] = []
        self.aborted_sessions: list[str] = []
        self.permission_calls: list[dict[str, Any]] = []
        self.session_lookup: dict[str, dict[str, Any]] = {}
        self._next_session_index = 0
        self.create_session_raises: Exception | None = None
        self.send_prompt_raises: Exception | None = None
        self.get_session_raises: Exception | None = None
        self.statuses: dict[str, dict[str, Any]] = {}

    def create_session(self, *, directory: str, title: str | None = None) -> OpencodeSessionInfo:
        if self.create_session_raises is not None:
            raise self.create_session_raises
        session_id = f"ses_fakehttp{self._next_session_index:08d}"
        self._next_session_index += 1
        self.created_sessions.append({"directory": directory, "title": title, "id": session_id})
        info = OpencodeSessionInfo(id=session_id, directory=directory, title=title or "")
        self.session_lookup[session_id] = {
            "id": session_id,
            "directory": directory,
            "title": title or "",
        }
        return info

    def get_session(self, session_id: str, *, directory: str | None = None) -> dict[str, Any]:
        if self.get_session_raises is not None:
            raise self.get_session_raises
        if session_id not in self.session_lookup:
            raise OpencodeHttpError(
                status=404,
                body=f"NotFoundError: Session not found: {session_id}",
                url=f"{self.base_url}/session/{session_id}",
            )
        return dict(self.session_lookup[session_id])

    def session_status(self, *, directory: str | None = None) -> dict[str, dict[str, Any]]:
        return dict(self.statuses)

    def send_prompt_async(
        self,
        session_id: str,
        *,
        prompt_text: str,
        directory: str | None = None,
        agent: str | None = None,
    ) -> None:
        if self.send_prompt_raises is not None:
            raise self.send_prompt_raises
        self.prompt_calls.append({
            "session_id": session_id,
            "prompt_text": prompt_text,
            "directory": directory,
            "agent": agent,
        })

    def abort_session(self, session_id: str, *, directory: str | None = None) -> bool:
        self.aborted_sessions.append(session_id)
        return True

    def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        *,
        response: str,
        directory: str | None = None,
    ) -> bool:
        self.permission_calls.append({
            "session_id": session_id,
            "permission_id": permission_id,
            "response": response,
        })
        return True

    def stream_events(
        self,
        *,
        directory: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        if stop_event is not None:
            stop_event.wait()
        return iter(())


class StubConsumer:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.aborted: list[str] = []
        self.session_log_paths: dict[str, Path] = {}
        self.session_directories: dict[str, str] = {}
        self.start_count = 0
        self.stop_count = 0
        self._states: dict[str, CompletionState] = {}

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def register_session(
        self,
        session_id: str,
        *,
        directory: str | None = None,
        event_log_path: Path | None = None,
    ) -> CompletionState:
        self.registered.append(session_id)
        if event_log_path is not None:
            self.session_log_paths[session_id] = event_log_path
        if directory is not None:
            self.session_directories[session_id] = directory
        state = self._states.setdefault(session_id, CompletionState(sessionID=session_id))
        return state

    def unregister_session(self, session_id: str) -> None:
        self.unregistered.append(session_id)
        self._states.pop(session_id, None)

    def get_state(self, session_id: str) -> CompletionState | None:
        return self._states.get(session_id)

    def mark_aborted(self, session_id: str) -> None:
        self.aborted.append(session_id)
        state = self._states.get(session_id)
        if state is not None:
            state.aborted = True
            state.event.set()

    def complete_idle(self, session_id: str) -> None:
        state = self._states[session_id]
        state.event.set()

    def complete_error(self, session_id: str, message: str) -> None:
        state = self._states[session_id]
        state.error_message = message
        state.event.set()


@pytest.fixture
def runner_factory(tmp_path: Path):
    def _make(
        client: FakeOpencodeClient | None = None,
        consumer: StubConsumer | None = None,
        *,
        permission_response: str | None = None,
    ) -> tuple[OmoHttpRunner, FakeOpencodeClient, StubConsumer]:
        client_obj = client or FakeOpencodeClient()
        consumer_obj = consumer or StubConsumer()

        class _StubServerManager:
            def __init__(self) -> None:
                self.shutdown_called = False

            def get_server_for_repo(self, repo_path: Path) -> str:
                return client_obj.base_url

            def shutdown_all(self) -> None:
                self.shutdown_called = True

        runner = OmoHttpRunner(
            tmp_path / "runs",
            server_manager=_StubServerManager(),  # type: ignore[arg-type]
            client_factory=lambda _base_url: client_obj,
            consumer_factory=lambda _client, _log_path, _resp: consumer_obj,
            permission_response=permission_response,
        )
        return runner, client_obj, consumer_obj

    return _make


def test_launch_implementation_stage_prepends_ultrawork_prefix(runner_factory, tmp_path: Path) -> None:
    runner, client, consumer = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=1)

    session = runner.launch(tmp_path, job, "Implement Issue #1.")

    assert client.created_sessions and client.created_sessions[0]["directory"] == str(tmp_path)
    assert client.prompt_calls and client.prompt_calls[0]["session_id"] == client.created_sessions[0]["id"]
    assert client.prompt_calls[0]["prompt_text"] == "ultrawork\n\nImplement Issue #1."
    assert session.omx_session_id == client.created_sessions[0]["id"]
    assert consumer.registered == [session.omx_session_id]
    assert Path(session.prompt_path).read_text(encoding="utf-8") == "ultrawork\n\nImplement Issue #1."


def test_launch_issue_request_stage_skips_ultrawork_prefix(runner_factory, tmp_path: Path) -> None:
    runner, client, _ = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=1)

    runner.launch(tmp_path, job, "Investigate Issue #1.")

    assert client.prompt_calls[0]["prompt_text"] == "Investigate Issue #1."


def test_launch_issue_followup_stage_skips_ultrawork_prefix(runner_factory, tmp_path: Path) -> None:
    runner, client, _ = runner_factory()
    client.session_lookup["ses_already1234"] = {"id": "ses_already1234", "directory": str(tmp_path)}
    job = JobRecord(repo_full_name="acme/demo", stage="issue_followup", issue_number=2)

    runner.resume(tmp_path, job, "Refine plan based on follow-up.", "ses_already1234")

    assert client.prompt_calls[0]["prompt_text"] == "Refine plan based on follow-up."


def test_launch_does_not_double_prefix_when_prompt_already_starts_with_ultrawork(
    runner_factory, tmp_path: Path
) -> None:
    runner, client, _ = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=1)

    runner.launch(tmp_path, job, "ultrawork already leading")

    assert client.prompt_calls[0]["prompt_text"] == "ultrawork already leading"


def test_resume_validates_session_then_sends_prompt(runner_factory, tmp_path: Path) -> None:
    runner, client, consumer = runner_factory()
    client.session_lookup["ses_existingone1234"] = {"id": "ses_existingone1234", "directory": str(tmp_path)}
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=2)

    session = runner.resume(tmp_path, job, "Continue work", "ses_existingone1234")

    assert session.omx_session_id == "ses_existingone1234"
    assert client.prompt_calls[0]["session_id"] == "ses_existingone1234"
    assert client.prompt_calls[0]["prompt_text"] == "ultrawork\n\nContinue work"
    assert consumer.registered == ["ses_existingone1234"]


def test_resume_raises_rollout_missing_when_session_404(runner_factory, tmp_path: Path) -> None:
    runner, client, _ = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="issue_followup", issue_number=2)
    client.get_session_raises = OpencodeHttpError(
        status=404,
        body="NotFoundError: Session not found: ses_zzz",
        url="http://test-server/session/ses_zzz",
    )

    with pytest.raises(RolloutMissingError):
        runner.resume(tmp_path, job, "Continue", "ses_zzz")


def test_resume_raises_rollout_missing_when_opencode_400_invalid_session_id_format(
    runner_factory, tmp_path: Path
) -> None:
    runner, client, _ = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="issue_followup", issue_number=2)
    client.get_session_raises = OpencodeHttpError(
        status=400,
        body=(
            '{"data":{"sessionID":"019da0f4-6ef1-7923-811f-57eb3e93bd8e"},'
            '"error":[{"origin":"string","code":"invalid_format","format":"starts_with",'
            '"prefix":"ses","path":["sessionID"],"message":"Invalid string: must start with \\"ses\\""}],'
            '"success":false}'
        ),
        url="http://test-server/session/019da0f4-6ef1-7923-811f-57eb3e93bd8e",
    )

    with pytest.raises(RolloutMissingError):
        runner.resume(tmp_path, job, "Continue", "019da0f4-6ef1-7923-811f-57eb3e93bd8e")


def test_wait_returns_when_consumer_marks_idle(runner_factory, tmp_path: Path) -> None:
    runner, _, consumer = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=3)

    session = runner.launch(tmp_path, job, "hello")

    consumer.complete_idle(session.omx_session_id)
    runner.wait(session.runtime_handle, timeout_seconds=2)


def test_wait_raises_transient_capacity_error_from_session_error(runner_factory, tmp_path: Path) -> None:
    runner, _, consumer = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=4)

    session = runner.launch(tmp_path, job, "hi")
    consumer.complete_error(session.omx_session_id, "Selected model is at capacity, retry later.")

    with pytest.raises(TransientCapacityError):
        runner.wait(session.runtime_handle, timeout_seconds=2)


def test_wait_raises_rollout_missing_error_from_session_error(runner_factory, tmp_path: Path) -> None:
    client = FakeOpencodeClient()
    client.session_lookup["ses_doomedaa1234"] = {"id": "ses_doomedaa1234", "directory": str(tmp_path)}
    runner, _, consumer = runner_factory(client=client)
    job = JobRecord(repo_full_name="acme/demo", stage="issue_followup", issue_number=5)

    session = runner.resume(tmp_path, job, "go", "ses_doomedaa1234")
    consumer.complete_error(session.omx_session_id, "Session not found: ses_doomedaa1234")

    with pytest.raises(RolloutMissingError):
        runner.wait(session.runtime_handle, timeout_seconds=2)


def test_wait_times_out_when_completion_state_never_settles(runner_factory, tmp_path: Path) -> None:
    runner, _, _ = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=6)

    session = runner.launch(tmp_path, job, "hi")

    with pytest.raises(TimeoutError):
        runner.wait(session.runtime_handle, timeout_seconds=0.1)


def test_close_session_aborts_and_unregisters(runner_factory, tmp_path: Path) -> None:
    runner, client, consumer = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=7)

    session = runner.launch(tmp_path, job, "hi")
    runner.close_session(session.runtime_handle)

    assert session.omx_session_id in client.aborted_sessions
    assert session.omx_session_id in consumer.unregistered


def test_get_session_id_returns_id_after_launch(runner_factory, tmp_path: Path) -> None:
    runner, _, _ = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=8)

    session = runner.launch(tmp_path, job, "hi")

    assert runner.get_session_id(session.runtime_handle) == session.omx_session_id


def test_can_resume_accepts_ses_prefixed_ids(runner_factory, tmp_path: Path) -> None:
    runner, _, _ = runner_factory()

    assert runner.can_resume("ses_25afdf9c7ffekN3dovMQw6meL2") is True
    assert runner.can_resume("019da16a-565d-7c81-98c9-4b7ff38a3f9b") is False
    assert runner.can_resume("") is False


def test_send_prompt_failure_unregisters_session_state(runner_factory, tmp_path: Path) -> None:
    client = FakeOpencodeClient()
    client.send_prompt_raises = RuntimeError("network blip")
    runner, _, consumer = runner_factory(client=client)
    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=9)

    with pytest.raises(RuntimeError, match="network blip"):
        runner.launch(tmp_path, job, "hi")

    assert consumer.registered and consumer.unregistered == consumer.registered


def test_runner_writes_request_log_for_inspection(runner_factory, tmp_path: Path) -> None:
    runner, _, _ = runner_factory()
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=10)

    session = runner.launch(tmp_path, job, "Implement Issue #10.")

    request_payload = json.loads(Path(session.script_path).read_text(encoding="utf-8"))
    assert request_payload["session_id"] == session.omx_session_id
    assert request_payload["prompt_text"] == "ultrawork\n\nImplement Issue #10."


def test_event_consumer_auto_grants_permission_with_default_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeOpencodeClient()
    consumer = OpencodeEventConsumer(client)  # type: ignore[arg-type]
    consumer._handle_event({
        "type": "permission.updated",
        "properties": {"id": "perm_xyz", "sessionID": "ses_a"},
    })

    assert client.permission_calls == [
        {"session_id": "ses_a", "permission_id": "perm_xyz", "response": "once"},
    ]


def test_event_consumer_marks_idle_event_completes_state() -> None:
    client = FakeOpencodeClient()
    consumer = OpencodeEventConsumer(client)  # type: ignore[arg-type]
    state = consumer.register_session("ses_target")

    consumer._handle_event({"type": "session.idle", "properties": {"sessionID": "ses_target"}})

    assert state.event.is_set()
    assert state.error_message is None


def test_event_consumer_records_error_message_on_session_error() -> None:
    client = FakeOpencodeClient()
    consumer = OpencodeEventConsumer(client)  # type: ignore[arg-type]
    state = consumer.register_session("ses_err")

    consumer._handle_event({
        "type": "session.error",
        "properties": {
            "sessionID": "ses_err",
            "error": {"name": "ApiError", "data": {"message": "model is currently overloaded"}},
        },
    })

    assert state.event.is_set()
    assert state.error_message == "model is currently overloaded"


def test_event_consumer_uses_explicit_permission_response_argument() -> None:
    client = FakeOpencodeClient()
    consumer = OpencodeEventConsumer(client, permission_response="always")  # type: ignore[arg-type]
    consumer._handle_event({
        "type": "permission.updated",
        "properties": {"id": "perm_y", "sessionID": "ses_b"},
    })

    assert client.permission_calls[0]["response"] == "always"


def test_event_consumer_writes_jsonl_event_log(tmp_path: Path) -> None:
    client = FakeOpencodeClient()
    log_path = tmp_path / "events.jsonl"
    consumer = OpencodeEventConsumer(client, event_log_path=log_path)  # type: ignore[arg-type]

    consumer._handle_event({"type": "session.idle", "properties": {"sessionID": "ses_log"}})

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["type"] == "session.idle"
    assert payload["properties"]["sessionID"] == "ses_log"


def test_event_consumer_routes_per_session_events_to_registered_log(tmp_path: Path) -> None:
    client = FakeOpencodeClient()
    default_log = tmp_path / "default.jsonl"
    consumer = OpencodeEventConsumer(client, event_log_path=default_log)  # type: ignore[arg-type]
    session_log = tmp_path / "alpha.jsonl"
    consumer.register_session("ses_alpha", event_log_path=session_log)

    consumer._handle_event({"type": "session.idle", "properties": {"sessionID": "ses_alpha"}})
    consumer._handle_event({"type": "session.idle", "properties": {"sessionID": "ses_unrelated"}})

    alpha_lines = session_log.read_text(encoding="utf-8").splitlines()
    default_lines = default_log.read_text(encoding="utf-8").splitlines()
    assert len(alpha_lines) == 1
    assert json.loads(alpha_lines[0])["properties"]["sessionID"] == "ses_alpha"
    sids_in_default = [json.loads(line)["properties"].get("sessionID") for line in default_lines]
    assert "ses_alpha" in sids_in_default
    assert "ses_unrelated" in sids_in_default


def test_event_consumer_extracts_session_id_from_nested_info_payload(tmp_path: Path) -> None:
    client = FakeOpencodeClient()
    default_log = tmp_path / "default.jsonl"
    consumer = OpencodeEventConsumer(client, event_log_path=default_log)  # type: ignore[arg-type]
    child_log = tmp_path / "child.jsonl"
    consumer.register_session("ses_child", event_log_path=child_log)

    consumer._handle_event({
        "type": "session.created",
        "properties": {"info": {"id": "ses_child", "parentID": "ses_parent"}},
    })

    child_lines = child_log.read_text(encoding="utf-8").splitlines()
    assert len(child_lines) == 1


def test_event_consumer_unregister_clears_per_session_log_routing(tmp_path: Path) -> None:
    client = FakeOpencodeClient()
    default_log = tmp_path / "default.jsonl"
    consumer = OpencodeEventConsumer(client, event_log_path=default_log)  # type: ignore[arg-type]
    session_log = tmp_path / "alpha.jsonl"
    consumer.register_session("ses_alpha", event_log_path=session_log)
    consumer.unregister_session("ses_alpha")

    consumer._handle_event({"type": "session.idle", "properties": {"sessionID": "ses_alpha"}})

    assert not session_log.exists() or session_log.read_text(encoding="utf-8") == ""
    assert default_log.exists() and "ses_alpha" in default_log.read_text(encoding="utf-8")


class _DirectoryTrackingFakeClient(FakeOpencodeClient):
    def __init__(self) -> None:
        super().__init__()
        self.stream_events_calls: list[str | None] = []
        self.session_status_calls: list[str | None] = []

    def stream_events(
        self,
        *,
        directory: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        self.stream_events_calls.append(directory)
        if stop_event is not None:
            stop_event.wait()
        return iter(())

    def session_status(self, *, directory: str | None = None) -> dict[str, dict[str, Any]]:
        self.session_status_calls.append(directory)
        return super().session_status(directory=directory)


def test_event_consumer_spawns_sse_listener_per_registered_directory(tmp_path: Path) -> None:
    client = _DirectoryTrackingFakeClient()
    consumer = OpencodeEventConsumer(client)  # type: ignore[arg-type]
    consumer.start()
    repo_a = str(tmp_path / "repo-a")
    repo_b = str(tmp_path / "repo-b")
    try:
        consumer.register_session("ses_alpha", directory=repo_a)
        consumer.register_session("ses_beta", directory=repo_b)
        consumer.register_session("ses_alpha_followup", directory=repo_a)
        time.sleep(0.15)
    finally:
        consumer.stop()

    directories_listened = set(client.stream_events_calls)
    assert repo_a in directories_listened
    assert repo_b in directories_listened
    assert client.stream_events_calls.count(repo_a) == 1, (
        "should reuse the SSE listener for a directory that has multiple registered sessions"
    )


def test_event_consumer_does_not_listen_for_directoryless_registration() -> None:
    client = _DirectoryTrackingFakeClient()
    consumer = OpencodeEventConsumer(client)  # type: ignore[arg-type]
    consumer.start()
    try:
        consumer.register_session("ses_nodir")
        time.sleep(0.1)
    finally:
        consumer.stop()

    assert client.stream_events_calls == []


def test_event_consumer_auto_grant_forwards_directory_to_client(tmp_path: Path) -> None:
    client = FakeOpencodeClient()
    consumer = OpencodeEventConsumer(client)  # type: ignore[arg-type]
    repo_perm = str(tmp_path / "repo-perm")
    consumer.register_session("ses_perm", directory=repo_perm)

    consumer._handle_event({
        "type": "permission.updated",
        "properties": {"id": "perm_a", "sessionID": "ses_perm"},
    })

    assert client.permission_calls == [
        {"session_id": "ses_perm", "permission_id": "perm_a", "response": "once"},
    ]


def test_server_manager_returns_external_url_without_spawning(tmp_path: Path) -> None:
    manager = OpencodeServerManager(
        tmp_path / "runs",
        external_server_url="http://external.test:9999",
    )

    assert manager.get_server_for_repo(tmp_path) == "http://external.test:9999"


def test_server_manager_raises_when_repo_path_missing(tmp_path: Path) -> None:
    true_bin = "/usr/bin/true" if Path("/usr/bin/true").exists() else "/bin/true"
    manager = OpencodeServerManager(tmp_path / "runs", opencode_bin=true_bin)
    missing = tmp_path / "nope"

    with pytest.raises(OpencodeServerError, match="does not exist"):
        manager.get_server_for_repo(missing)


def test_server_manager_raises_when_subprocess_exits_before_ready(tmp_path: Path) -> None:
    false_bin = "/usr/bin/false" if Path("/usr/bin/false").exists() else "/bin/false"
    manager = OpencodeServerManager(
        tmp_path / "runs",
        opencode_bin=false_bin,
        ready_timeout_seconds=2.0,
    )

    with pytest.raises(OpencodeServerError):
        manager.get_server_for_repo(tmp_path)


def test_opencode_client_extract_sse_data_handles_multi_line_data() -> None:
    text = "data: foo\ndata: bar"

    assert OpencodeClient._extract_sse_data(text) == "foo\nbar"


def test_opencode_client_extract_sse_data_returns_none_when_no_data_lines() -> None:
    text = "event: ping\nretry: 1000"

    assert OpencodeClient._extract_sse_data(text) is None


def test_opencode_client_build_url_appends_query_string() -> None:
    client = OpencodeClient("http://x.test/")
    url = client._build_url("/session", {"directory": "/private/var/repo"})

    assert url == "http://x.test/session?directory=%2Fprivate%2Fvar%2Frepo"


def test_opencode_client_respond_permission_swallows_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpencodeClient("http://x.test")

    def fake_request(
        method: str, path: str, *, query: dict | None = None, body: dict | None = None, expect_json: bool = True
    ):
        del method, path, query, body, expect_json
        raise OpencodeHttpError(status=404, body="not found", url="http://x.test")

    monkeypatch.setattr(client, "_request", fake_request)

    assert client.respond_permission("ses_a", "perm_z", response="once") is False


def test_opencode_client_respond_permission_rejects_invalid_response() -> None:
    client = OpencodeClient("http://x.test")

    with pytest.raises(ValueError, match="invalid permission response"):
        client.respond_permission("ses_a", "perm_b", response="bogus")


def test_dani_service_runs_issue_request_through_omo_http_runner_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typing import cast

    from dani.github import GitHubCLI
    from dani.models import DaniConfig, NormalizedEvent
    from dani.opencode_http import OpencodeServerManager
    from dani.service import DaniService
    from dani.storage import JsonStorage
    from tests.helpers import FakeGitDevSyncer, FakeGitHubCLI

    github_stub = FakeGitHubCLI()
    fake_client = FakeOpencodeClient()
    stub_consumer = StubConsumer()

    class _StubServerManager(OpencodeServerManager):
        def get_server_for_repo(self, repo_path: Path) -> str:
            return fake_client.base_url

        def shutdown_all(self) -> None:
            return None

    monkeypatch.delenv("DANI_OMO_LEGACY_SUBPROCESS", raising=False)

    config = DaniConfig(
        data_dir=tmp_path / ".dani",
        webhook_secret="unit-test-secret",
        agent_runtime="omo",
    )
    storage = JsonStorage(config)

    runner = OmoHttpRunner(
        config.run_dir,
        server_manager=_StubServerManager(config.run_dir),
        client_factory=lambda _base_url: fake_client,
        consumer_factory=lambda _client, _log, _resp: stub_consumer,
    )

    def _post_signature_after_launch(*, session_id: str, prompt_text: str, **_: Any) -> None:
        signature = next(
            (line for line in prompt_text.splitlines() if line.startswith("<!-- dani:")),
            None,
        )
        if signature is not None:
            github_stub.issue_comment_map.setdefault(("acme/demo", 99), []).append({"body": signature})

        def _settle() -> None:
            time.sleep(0.05)
            stub_consumer.complete_idle(session_id)

        threading.Thread(target=_settle, daemon=True).start()

    original_send = fake_client.send_prompt_async

    def _instrumented_send(
        session_id: str, *, prompt_text: str, directory: str | None = None, agent: str | None = None
    ) -> None:
        original_send(session_id, prompt_text=prompt_text, directory=directory, agent=agent)
        _post_signature_after_launch(session_id=session_id, prompt_text=prompt_text)

    fake_client.send_prompt_async = _instrumented_send  # type: ignore[method-assign]

    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github_stub),
        dev_syncer=FakeGitDevSyncer(),
        omx_runner=runner,
    )
    service.register_repo("acme/demo", str(tmp_path))

    assert isinstance(service.omx_runner, OmoHttpRunner)

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=99,
            actor_login="human",
            payload={},
            body="Investigate runtime selection over HTTP",
            title="HTTP runtime smoke",
        )
    )
    service.wait_for_idle()

    job = storage.list_jobs()[0]
    assert job.status == "completed", f"expected completed, got {job.status} / {job.metadata!r}"

    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].omx_session_id == fake_client.created_sessions[0]["id"]
    assert not fake_client.prompt_calls[0]["prompt_text"].startswith("ultrawork"), (
        "issue_request is a planning stage; ultrawork prefix must not be auto-prepended"
    )
