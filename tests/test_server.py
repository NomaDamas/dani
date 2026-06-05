import hashlib
import hmac
import json
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from dani.agent_runner import AgentRunner
from dani.git_sync import DevSyncOutcome
from dani.github import GitHubCLI
from dani.models import DaniConfig
from dani.server import create_app
from dani.service import DaniService
from dani.storage import JsonStorage
from tests.helpers import FakeCodexRunner, FakeGitHubCLI

TEST_SECRET = "unit-test-secret"


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_github_webhook_endpoint_accepts_valid_signature(tmp_path: Path) -> None:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    github = FakeGitHubCLI()
    codex_runner = FakeCodexRunner(github)
    service = DaniService(
        config,
        storage=JsonStorage(config),
        github=cast(GitHubCLI, github),
        codex_runner=cast(AgentRunner, codex_runner),
    )
    service.register_repo("acme/demo", str(tmp_path))
    client = TestClient(create_app(service))
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/demo"},
        "issue": {"number": 3, "title": "Need it", "body": "Please"},
        "sender": {"login": "human"},
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post(
        "/webhook",
        content=body,
        headers={
            "x-github-event": "issues",
            "x-hub-signature-256": _signature(TEST_SECRET, body),
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_github_webhook_endpoint_queues_dev_sync_on_main_push(tmp_path: Path) -> None:
    class FakeSyncer:
        def sync(self, repo: object, job: object) -> DevSyncOutcome:
            return DevSyncOutcome(status="merged")

    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    github = FakeGitHubCLI()
    codex_runner = FakeCodexRunner(github)
    service = DaniService(
        config,
        storage=JsonStorage(config),
        github=cast(GitHubCLI, github),
        codex_runner=cast(AgentRunner, codex_runner),
        dev_syncer=FakeSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))
    client = TestClient(create_app(service))
    payload = {
        "ref": "refs/heads/main",
        "after": "abc123",
        "deleted": False,
        "repository": {"full_name": "acme/demo"},
        "sender": {"login": "human"},
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post(
        "/webhook",
        content=body,
        headers={
            "x-github-event": "push",
            "x-hub-signature-256": _signature(TEST_SECRET, body),
        },
    )

    service.wait_for_idle()

    assert response.status_code == 200
    assert response.json()["stage"] == "dev_sync"


def test_github_webhook_endpoint_dedupes_duplicate_external_pr_delivery(tmp_path: Path) -> None:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    github = FakeGitHubCLI()
    codex_runner = FakeCodexRunner(github)
    service = DaniService(
        config,
        storage=JsonStorage(config),
        github=cast(GitHubCLI, github),
        codex_runner=cast(AgentRunner, codex_runner),
    )
    service.register_repo("acme/demo", str(tmp_path))
    client = TestClient(create_app(service))
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "acme/demo"},
        "sender": {"login": "contributor"},
        "pull_request": {
            "number": 21,
            "title": "Feature/#21",
            "body": "Implements #21",
            "base": {"ref": "dev"},
            "head": {"ref": "feature/#21", "sha": "sha-21-2"},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "x-github-event": "pull_request",
        "x-github-delivery": "delivery-21",
        "x-hub-signature-256": _signature(TEST_SECRET, body),
    }

    first = client.post("/webhook", content=body, headers=headers)
    second = client.post("/webhook", content=body, headers=headers)
    service.wait_for_idle()

    assert first.status_code == 200
    assert first.json()["stage"] == "review_round"
    assert second.status_code == 200
    assert second.json() == {"status": "ignored", "reason": "duplicate_external_pr_event"}
    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=21)
    assert [job.review_round for job in review_jobs] == [1]
