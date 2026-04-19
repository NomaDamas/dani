from __future__ import annotations

import atexit
import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

OPENCODE_BIN_DEFAULT = "opencode"
SERVER_LISTENING_PATTERN = re.compile(r"opencode server listening on (http://\S+)")
SERVER_READY_TIMEOUT_SECONDS = 30.0
SERVER_SHUTDOWN_GRACE_SECONDS = 5.0


class OpencodeServerError(RuntimeError):
    pass


@dataclass(slots=True)
class _ServerEntry:
    base_url: str
    repo_path: Path
    process: subprocess.Popen[str] | None
    log_path: Path | None
    log_thread: threading.Thread | None = None
    log_buffer: list[str] = field(default_factory=list)


class OpencodeServerManager:
    def __init__(
        self,
        run_dir: Path,
        *,
        opencode_bin: str = OPENCODE_BIN_DEFAULT,
        external_server_url: str | None = None,
        ready_timeout_seconds: float = SERVER_READY_TIMEOUT_SECONDS,
    ) -> None:
        self.run_dir = run_dir
        self.opencode_bin = opencode_bin
        self.external_server_url = (external_server_url or "").strip() or None
        self._ready_timeout_seconds = ready_timeout_seconds
        self._lock = threading.RLock()
        self._servers: dict[str, _ServerEntry] = {}
        self._atexit_registered = False
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def get_server_for_repo(self, repo_path: Path) -> str:
        if self.external_server_url:
            return self.external_server_url
        key = self._cache_key(repo_path)
        with self._lock:
            entry = self._servers.get(key)
            if entry is not None and self._is_alive(entry):
                return entry.base_url
            if entry is not None:
                logger.warning("opencode server for %s exited; respawning", key)
                self._dispose_locked(entry)
                self._servers.pop(key, None)
            entry = self._spawn_server(Path(key))
            self._servers[key] = entry
            self._register_atexit_locked()
            return entry.base_url

    def shutdown_all(self) -> None:
        with self._lock:
            entries = list(self._servers.values())
            self._servers.clear()
        for entry in entries:
            self._dispose_locked(entry)

    def _spawn_server(self, repo_path: Path) -> _ServerEntry:
        if not repo_path.exists():
            msg = f"opencode server repo path does not exist: {repo_path}"
            raise OpencodeServerError(msg)
        log_path = self.run_dir / f"opencode-server-{repo_path.name}-{os.getpid()}-{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(  # noqa: S603
            [self.opencode_bin, "serve", "--port", "0", "--hostname", "127.0.0.1", "--print-logs"],
            cwd=str(repo_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        entry = _ServerEntry(
            base_url="",
            repo_path=repo_path,
            process=process,
            log_path=log_path,
        )
        base_url = self._await_ready_url(entry)
        entry.base_url = base_url
        entry.log_thread = self._start_log_pump(entry)
        logger.info("opencode server ready for %s at %s (pid=%s)", repo_path, base_url, process.pid)
        return entry

    def _await_ready_url(self, entry: _ServerEntry) -> str:
        process = entry.process
        if process is None or process.stdout is None:
            msg = "opencode server subprocess has no stdout pipe"
            raise OpencodeServerError(msg)
        deadline = time.monotonic() + self._ready_timeout_seconds
        log_handle = entry.log_path.open("w", encoding="utf-8") if entry.log_path else None
        try:
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if line == "":
                    if process.poll() is not None:
                        msg = self._format_startup_failure(entry)
                        raise OpencodeServerError(msg)
                    time.sleep(0.05)
                    continue
                entry.log_buffer.append(line)
                if log_handle is not None:
                    log_handle.write(line)
                    log_handle.flush()
                match = SERVER_LISTENING_PATTERN.search(line)
                if match:
                    return match.group(1)
            msg = (
                f"opencode server did not announce ready URL within {self._ready_timeout_seconds:.0f}s; "
                f"log: {entry.log_path}"
            )
            raise OpencodeServerError(msg)
        finally:
            if log_handle is not None:
                log_handle.close()

    def _start_log_pump(self, entry: _ServerEntry) -> threading.Thread:
        process = entry.process
        log_path = entry.log_path
        if process is None or process.stdout is None or log_path is None:
            return threading.Thread(target=lambda: None, daemon=True)
        stream = process.stdout

        def _pump() -> None:
            try:
                with log_path.open("a", encoding="utf-8") as log_handle:
                    for line in stream:
                        log_handle.write(line)
                        log_handle.flush()
            except Exception:
                logger.debug("opencode server log pump exited", exc_info=True)

        thread = threading.Thread(target=_pump, daemon=True, name=f"opencode-server-log-{process.pid}")
        thread.start()
        return thread

    def _format_startup_failure(self, entry: _ServerEntry) -> str:
        process = entry.process
        exit_code = process.poll() if process is not None else None
        cmd = " ".join(
            shlex.quote(part) for part in [self.opencode_bin, "serve", "--port", "0", "--hostname", "127.0.0.1"]
        )
        tail = "".join(entry.log_buffer[-20:]) or "(no output)"
        return f"opencode server exited before ready (exit_code={exit_code}, cmd={cmd}); tail:\n{tail}"

    def _is_alive(self, entry: _ServerEntry) -> bool:
        process = entry.process
        return process is not None and process.poll() is None

    def _dispose_locked(self, entry: _ServerEntry) -> None:
        process = entry.process
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=SERVER_SHUTDOWN_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.kill()
                    process.wait(timeout=SERVER_SHUTDOWN_GRACE_SECONDS)
            except Exception:
                logger.warning("error terminating opencode server pid=%s", process.pid, exc_info=True)
        if entry.log_thread is not None and entry.log_thread.is_alive():
            entry.log_thread.join(timeout=1.0)

    def _register_atexit_locked(self) -> None:
        if self._atexit_registered:
            return
        atexit.register(self.shutdown_all)
        self._atexit_registered = True

    @staticmethod
    def _cache_key(repo_path: Path) -> str:
        return str(repo_path.resolve())
