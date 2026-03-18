import hashlib
import hmac
import json
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from dani.github import GitHubCLI
from dani.models import DaniConfig
from dani.omx_runner import OmxRunner
from dani.server import create_app
from dani.service import DaniService
from dani.storage import JsonStorage
from tests.helpers import FakeGitHubCLI, FakeOmxRunner

TEST_SECRET = "unit-test-secret"


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_github_webhook_endpoint_accepts_valid_signature(tmp_path: Path) -> None:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    github = FakeGitHubCLI()
    omx_runner = FakeOmxRunner(github)
    service = DaniService(
        config, storage=JsonStorage(config), github=cast(GitHubCLI, github), omx_runner=cast(OmxRunner, omx_runner)
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
        "/webhooks/github",
        content=body,
        headers={
            "x-github-event": "issues",
            "x-hub-signature-256": _signature(TEST_SECRET, body),
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
