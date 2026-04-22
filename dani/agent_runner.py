from __future__ import annotations

from pathlib import Path
from typing import Protocol, TextIO, runtime_checkable

from dani.models import JobRecord, SessionRecord

RUNTIME_ALIASES: dict[str, str] = {
    "omx": "omx",
    "oh-my-codex": "omx",
    "codex": "omx",
    "omo": "omo",
    "oh-my-openagents": "omo",
    "oh-my-openagent": "omo",
    "opencode": "omo",
}


class ManagedProcess(Protocol):
    def poll(self) -> object: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> object: ...
    def kill(self) -> None: ...


ProcessEntry = tuple[ManagedProcess, TextIO, TextIO]


@runtime_checkable
class AgentRunner(Protocol):
    """Protocol every dani agent runtime adapter must satisfy.

    Implementations wrap an external agent CLI (omx, opencode, ...) and expose
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
        omx_session_id: str,
    ) -> SessionRecord: ...

    def wait(
        self,
        runtime_handle: str,
        *,
        poll_interval: float = 0.5,
        timeout_seconds: float = 1800,
    ) -> None: ...

    def close_session(self, runtime_handle: str) -> None: ...

    def get_session_id(self, runtime_handle: str) -> str | None: ...


def normalize_runtime(runtime: str | None) -> str:
    normalized = (runtime or "omx").strip().lower()
    if normalized in RUNTIME_ALIASES:
        return RUNTIME_ALIASES[normalized]
    msg = f"unknown agent runtime: {runtime!r} (expected 'omx' or 'omo')"
    raise ValueError(msg)


def build_agent_runner(runtime: str, run_dir: Path) -> AgentRunner:
    """Factory returning the AgentRunner matching *runtime* (``omx`` or ``omo``)."""
    from dani.omo_runner import OmoRunner
    from dani.omx_runner import OmxRunner

    normalized = normalize_runtime(runtime)
    if normalized == "omx":
        return OmxRunner(run_dir)
    if normalized == "omo":
        return OmoRunner(run_dir)
    msg = f"unknown agent runtime: {runtime!r} (expected 'omx' or 'omo')"
    raise ValueError(msg)
