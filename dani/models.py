from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(slots=True)
class RepoConfig:
    full_name: str
    local_path: str
    main_branch: str = "main"
    dev_branch: str = "dev"
    enabled: bool = True
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class JobRecord:
    repo_full_name: str
    stage: str
    issue_number: int | None = None
    pr_number: int | None = None
    review_round: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "queued"
    session_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SessionRecord:
    repo_full_name: str
    stage: str
    runtime_handle: str
    prompt_path: str
    script_path: str
    worktree_path: str
    job_id: str
    issue_number: int | None = None
    pr_number: int | None = None
    review_round: int | None = None
    omx_session_id: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "launched"
    ended_at: str | None = None
    termination_reason: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NormalizedEvent:
    kind: str
    repo_full_name: str
    action: str
    number: int
    actor_login: str
    payload: dict[str, Any]
    body: str | None = None
    title: str | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    is_pull_request: bool = False


@dataclass(slots=True)
class DaniConfig:
    data_dir: Path
    webhook_secret: str
    host: str = "127.0.0.1"
    port: int = 8787
    review_rounds: int = 3

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.json"

    @property
    def jobs_path(self) -> Path:
        return self.data_dir / "jobs.json"

    @property
    def sessions_path(self) -> Path:
        return self.data_dir / "sessions.json"

    @property
    def events_path(self) -> Path:
        return self.data_dir / "events.jsonl"

    @property
    def processed_events_path(self) -> Path:
        return self.data_dir / "processed-events.json"

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "runs"
