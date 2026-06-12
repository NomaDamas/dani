from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from dani.models import (
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    RUNTIME_AUTO,
    RUNTIME_CODEX,
    RUNTIME_GAJAE,
    JobRecord,
    SessionRecord,
)

RUNTIME_ALIASES: dict[str, str] = {
    RUNTIME_AUTO: RUNTIME_AUTO,
    RUNTIME_CODEX: RUNTIME_CODEX,
    RUNTIME_GAJAE: RUNTIME_GAJAE,
    "gajae-code": RUNTIME_GAJAE,
    "gjc": RUNTIME_GAJAE,
}


class ManagedProcess(Protocol):
    def poll(self) -> object: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> object: ...
    def kill(self) -> None: ...


@runtime_checkable
class AgentRunner(Protocol):
    """Protocol every dani agent runtime adapter must satisfy.

    Implementations wrap an external agent CLI (codex, gjc, ...) and expose
    a uniform interface to ``DaniService`` for launching a new agent session,
    resuming an existing one, waiting for completion, and releasing process
    resources.
    """

    run_dir: Path

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord: ...

    def resume(
        self,
        repo_path: Path,
        job: JobRecord,
        prompt: str,
        codex_session_id: str,
    ) -> SessionRecord: ...

    def wait(
        self,
        runtime_handle: str,
        *,
        poll_interval: float = 0.5,
        timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
    ) -> None: ...

    def close_session(self, runtime_handle: str) -> None: ...

    def get_session_id(self, runtime_handle: str) -> str | None: ...

    def can_resume(self, session_id: str) -> bool: ...


def normalize_runtime(runtime: str | None) -> str:
    normalized = (runtime or RUNTIME_CODEX).strip().lower()
    if normalized in RUNTIME_ALIASES:
        return RUNTIME_ALIASES[normalized]
    msg = f"unknown agent runtime: {runtime!r} (expected 'auto', 'codex', or 'gajae')"
    raise ValueError(msg)


def build_agent_runner(runtime: str, run_dir: Path) -> AgentRunner:
    from dani.codex_runner import CodexRunner
    from dani.gajae_runner import GajaeRunner

    normalized = normalize_runtime(runtime)
    if normalized == RUNTIME_AUTO:
        normalized = RUNTIME_CODEX
    if normalized == RUNTIME_CODEX:
        return CodexRunner(run_dir)
    if normalized == RUNTIME_GAJAE:
        return GajaeRunner(run_dir)
    msg = f"unknown agent runtime: {runtime!r} (expected 'auto', 'codex', or 'gajae')"
    raise ValueError(msg)
