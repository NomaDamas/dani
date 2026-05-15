"""Read-only health diagnostics for a dani installation.

`dani doctor` is a Typer subcommand that inspects a dani installation and
reports on configuration, storage integrity, registered repositories, agent
runtime binaries, GitHub auth, the local webhook server, stuck jobs/sessions,
disk usage, accumulated backups, and process sprawl.

Sacred contract (enforced by tests):

1. READ-ONLY — never mutates ``~/.dani/``. No ``--fix``, no writes, no
   ``JsonStorage(config)`` (which creates dirs and skeleton files on init), no
   ``build_service()``, no runner construction. Doctor reads JSON files
   directly via ``read_only_snapshot``.
2. NON-DISRUPTIVE — must work while ``dani serve`` is live. No port binding,
   no storage write locks, no competing subprocesses. Tolerates transient
   parse errors via one retry (atomic-replace race with the live writer).
3. STDLIB + existing deps only. No new dependencies.
4. CROSS-PLATFORM — darwin + linux (no Windows).
5. NO SECRET LEAKAGE — never prints any value of ``DANI_WEBHOOK_SECRET``,
   ``DANI_GITHUB_TOKEN``, ``GITHUB_TOKEN``, ``GH_TOKEN``, ``GITHUB_PAT``.
   Never prints raw ``ps`` argv (OMX argv contains full agent prompts).
6. NO ``--fix`` IN v1 — separate future command.

Exit codes:

- ``0``: overall ``ok`` (or ``warn`` without ``--strict``, or all checks
  skipped)
- ``1``: overall ``warn`` AND ``--strict``
- ``2``: overall ``fail``
- ``3``: doctor itself crashed (unhandled exception)
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# ============================================================
# 1. Types
# ============================================================


class CheckStatus(str, Enum):
    """Severity classification for a single check or for the overall report.

    ``SKIP`` is neutral: it does not elevate the overall severity above ``OK``.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    """Outcome of running a single doctor check."""

    name: str
    status: CheckStatus
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    error: str | None = None


@dataclass
class DoctorReport:
    """Aggregated doctor report serialized to text or JSON."""

    schema_version: int = 1
    started_at: str = ""
    finished_at: str = ""
    data_dir: str = ""
    host: dict[str, Any] = field(default_factory=dict)
    overall_status: CheckStatus = CheckStatus.OK
    summary: dict[str, int] = field(default_factory=dict)
    results: list[CheckResult] = field(default_factory=list)


@dataclass
class CheckContext:
    """Shared, read-only state passed to every check.

    Every field is computed ONCE at the start of ``run_doctor`` and never
    mutated by a check. Checks may read but must not modify any field.
    """

    data_dir: Path
    config_parsed: dict[str, Any] | None
    config_parse_error: str | None
    snapshot: dict[str, Any]
    snapshot_errors: dict[str, str]
    env: Mapping[str, str]
    now_utc: datetime
    timeout_seconds: float
    verbose: bool
    thresholds: dict[str, int]
    port: int
    no_color: bool


# ============================================================
# 2. Constants
# ============================================================


SECRET_ENV_KEYS = frozenset({
    "DANI_WEBHOOK_SECRET",
    "DANI_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
})

# Token resolution priority: matches dani.github.GitHubCLI._resolve_token order.
GITHUB_TOKEN_KEYS_PRIORITY: tuple[str, ...] = (
    "DANI_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
)

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "superseded"})

SNAPSHOT_FILES: dict[str, str] = {
    "registry": "registry.json",
    "jobs": "jobs.json",
    "sessions": "sessions.json",
    "processed_events": "processed-events.json",
    "terminal_targets": "terminal-targets.json",
}
EVENTS_JSONL = "events.jsonl"
CONFIG_FILE = "config.json"

REQUIRED_SNAPSHOT_KEYS = frozenset({"registry", "jobs", "sessions"})

# Whitelisted --threshold keys. Unknown keys raise BadParameter.
ALLOWED_THRESHOLD_KEYS = frozenset({
    "jobs_bytes_warn",
    "jobs_bytes_fail",
    "sessions_bytes_warn",
    "sessions_bytes_fail",
    "events_bytes_warn",
    "runs_bytes_warn",
    "runs_bytes_fail",
    "stuck_job_age_seconds",
    "stuck_session_age_seconds",
    "backup_count_warn",
    "backup_age_days_warn",
    "backup_bytes_warn",
    "process_sprawl_count_warn",
})


# ============================================================
# 3. Redaction + token resolution
# ============================================================


def redact_secrets(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of *env* with values for known secret keys redacted.

    Known secret keys (``SECRET_ENV_KEYS``) are mapped to ``"<set>"`` if their
    value is non-empty, else ``"<unset>"``. All other keys are preserved
    verbatim. The returned dict only includes the secret keys (callers needing
    a full env should layer ``dict(env) | redact_secrets(env)`` themselves).
    """

    redacted: dict[str, str] = {}
    for key in SECRET_ENV_KEYS:
        value = env.get(key, "")
        redacted[key] = "<set>" if value else "<unset>"
    return redacted


def resolve_github_token(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Resolve the active GitHub token from environment variables.

    Returns ``(token, source_env_var_name)``. ``token`` is the actual secret
    value or ``None`` if no candidate env var is set to a non-empty value.
    ``source_env_var_name`` is the name of the env var that supplied the
    token, or ``None`` if no token is available.

    The token value MUST NEVER be logged or rendered into doctor output.
    Only the source name is safe to include.
    """

    for key in GITHUB_TOKEN_KEYS_PRIORITY:
        value = env.get(key, "")
        if value:
            return value, key
    return None, None


# ============================================================
# 4. Read-only snapshot (replaces JsonStorage entirely)
# ============================================================


def _read_json_file(path: Path, *, retry_delay_s: float) -> tuple[Any, str | None, bool]:
    """Read and parse a JSON file with one transient-error retry.

    Returns ``(parsed_or_none, error_msg, retry_used)``. On success returns
    ``(value, None, retry_used)``. If the file is missing, returns
    ``(None, "missing", False)``. On ``JSONDecodeError``, sleeps
    ``retry_delay_s`` and retries once; if the retry succeeds, returns
    ``(value, None, True)``. If both attempts fail, returns
    ``(None, error_msg, True)``.

    NEVER writes, mkdirs, or otherwise mutates the filesystem.
    """

    if not path.exists():
        return None, "missing", False
    for attempt in (0, 1):
        try:
            text = path.read_text(encoding="utf-8")
            return json.loads(text), None, attempt > 0
        except json.JSONDecodeError as exc:
            if attempt == 0:
                time.sleep(retry_delay_s)
                continue
            return None, f"JSONDecodeError: {exc}", True
        except OSError as exc:
            return None, f"OSError: {exc}", attempt > 0
    return None, "unknown error", True


def _read_events_jsonl(path: Path, *, retry_delay_s: float) -> dict[str, Any]:
    """Inspect ``events.jsonl`` (append-only, NOT atomic-replaced).

    Captures size, line count, and parses only the first and last line. The
    last line may be transiently incomplete during an append by ``dani serve``;
    on parse error we retry once after ``retry_delay_s`` and, if still bad,
    set ``last_line_invalid=True`` (callers should treat this as WARN, not
    FAIL).

    Returns a metadata dict with keys: ``exists``, ``size_bytes``,
    ``line_count``, ``first_line_parsed``, ``last_line_parsed``,
    ``last_line_invalid``, ``error``.
    """

    meta: dict[str, Any] = {
        "exists": False,
        "size_bytes": 0,
        "line_count": 0,
        "first_line_parsed": False,
        "last_line_parsed": False,
        "last_line_invalid": False,
        "error": None,
    }
    if not path.exists():
        meta["error"] = "missing"
        return meta
    try:
        meta["exists"] = True
        meta["size_bytes"] = path.stat().st_size
    except OSError as exc:
        meta["error"] = f"OSError: {exc}"
        return meta

    try:
        line_count, first, last = _scan_events_lines(path)
    except OSError as exc:
        meta["error"] = f"OSError: {exc}"
        return meta

    meta["line_count"] = line_count
    meta["first_line_parsed"] = _is_json_line(first)
    last_ok = _is_json_line(last)
    if last is not None and not last_ok:
        time.sleep(retry_delay_s)
        try:
            _, _, last2 = _scan_events_lines(path)
        except OSError:
            last2 = None
        last_ok = _is_json_line(last2)
        meta["last_line_invalid"] = not last_ok
    meta["last_line_parsed"] = last_ok
    return meta


def _scan_events_lines(path: Path) -> tuple[int, str | None, str | None]:
    first: str | None = None
    last: str | None = None
    line_count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            line_count += 1
            if first is None:
                first = stripped
            last = stripped
    return line_count, first, last


def _is_json_line(text: str | None) -> bool:
    if text is None:
        return False
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def read_only_snapshot(data_dir: Path, *, retry_delay_s: float = 0.1) -> tuple[dict[str, Any], dict[str, str]]:
    """Read every storage file under *data_dir* without mutating anything.

    Returns ``(snapshot, errors)``. ``snapshot`` is a dict keyed by the
    storage-file logical name (``registry``, ``jobs``, ``sessions``,
    ``processed_events``, ``terminal_targets``, plus ``events_jsonl_meta``).
    Values are the parsed JSON payloads (or ``None`` if unreadable). For
    ``events.jsonl`` (append-only), the value is a metadata dict.

    ``errors`` maps logical names to their parse error message when applicable.

    NEVER calls ``JsonStorage``, NEVER mkdirs, NEVER writes. Honors the
    atomic-replace contract used by dani's storage layer.
    """

    snapshot: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for key, filename in SNAPSHOT_FILES.items():
        path = data_dir / filename
        value, error, _retry_used = _read_json_file(path, retry_delay_s=retry_delay_s)
        snapshot[key] = value
        if error is not None:
            errors[key] = error

    events_path = data_dir / EVENTS_JSONL
    snapshot["events_jsonl_meta"] = _read_events_jsonl(events_path, retry_delay_s=retry_delay_s)
    events_error = snapshot["events_jsonl_meta"].get("error")
    if events_error is not None:
        errors["events_jsonl"] = events_error

    return snapshot, errors


def read_only_config(data_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read ``~/.dani/config.json`` without raising.

    Returns ``(parsed, error)``. If the file is absent, returns
    ``(None, None)`` (missing config is normal — env vars supply defaults).
    On JSON parse error or non-object top-level value, returns
    ``(None, error_message)``. Never mutates the filesystem.
    """

    path = data_dir / CONFIG_FILE
    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"OSError: {exc}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc}"
    if not isinstance(parsed, dict):
        return None, "top-level JSON value must be an object"
    return parsed, None


# ============================================================
# 5. Check registry
# ============================================================


CHECK_REGISTRY: dict[str, Callable[[CheckContext], CheckResult]] = {}


def register_check(
    name: str,
) -> Callable[[Callable[[CheckContext], CheckResult]], Callable[[CheckContext], CheckResult]]:
    """Decorator registering a check function under *name*.

    Each check is a callable ``(ctx: CheckContext) -> CheckResult``. Names
    must be unique within ``CHECK_REGISTRY``; duplicate registration raises
    ``ValueError``.
    """

    def _wrap(func: Callable[[CheckContext], CheckResult]) -> Callable[[CheckContext], CheckResult]:
        if name in CHECK_REGISTRY:
            msg = f"duplicate check registration: {name!r}"
            raise ValueError(msg)
        CHECK_REGISTRY[name] = func
        return func

    return _wrap


# ============================================================
# 6. Helper utilities
# ============================================================


def cap_list(items: list[Any], *, limit: int) -> tuple[list[Any], int]:
    """Return ``(capped_items, overflow_count)``.

    If ``len(items) <= limit``, returns ``(items, 0)``. Otherwise returns the
    first ``limit`` items plus the count of items dropped.
    """

    if limit < 0:
        limit = 0
    if len(items) <= limit:
        return list(items), 0
    return list(items[:limit]), len(items) - limit


def cap_str(text: object, *, limit: int = 200) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    if limit <= 1:
        return s[:limit]
    return s[: limit - 1] + "…"


def safe_iso_parse(value: object) -> datetime | None:
    """Parse an ISO 8601 timestamp; return ``None`` on any failure.

    Handles trailing ``Z`` (Python <3.11 quirk) by mapping to ``+00:00``.
    """

    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


# ============================================================
# 7. Severity aggregation
# ============================================================


SEVERITY_RANK: dict[CheckStatus, int] = {
    CheckStatus.OK: 0,
    CheckStatus.SKIP: 0,
    CheckStatus.WARN: 1,
    CheckStatus.FAIL: 2,
}


def compute_overall(results: list[CheckResult]) -> CheckStatus:
    """Compute overall status from *results*.

    ``SKIP`` is neutral (does not elevate severity). If every check
    skipped, the overall status is ``SKIP`` (signals "nothing meaningfully
    ran"). Otherwise it is the max severity among ``OK``/``WARN``/``FAIL``.
    """

    if not results:
        return CheckStatus.OK
    ranks = [SEVERITY_RANK[r.status] for r in results]
    max_rank = max(ranks)
    if max_rank == 0:
        if all(r.status == CheckStatus.SKIP for r in results):
            return CheckStatus.SKIP
        return CheckStatus.OK
    if max_rank == 1:
        return CheckStatus.WARN
    return CheckStatus.FAIL


def _summary_counts(results: list[CheckResult]) -> dict[str, int]:
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status.value] += 1
    return counts


# ============================================================
# 8. Orchestrator
# ============================================================


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _dani_version() -> str:
    """Best-effort lookup of installed dani version. Never raises."""

    try:
        from importlib.metadata import version

        return version("dani")
    except Exception:
        return "unknown"


def _host_info(port: int) -> dict[str, Any]:
    import platform
    import sys

    return {
        "port": port,
        "platform": platform.system().lower(),
        "platform_release": platform.release(),
        "python_version": sys.version.split()[0],
        "dani_version": _dani_version(),
    }


def run_doctor(
    data_dir: Path,
    *,
    check_names: list[str] | None = None,
    verbose: bool = False,
    timeout: float = 5.0,
    thresholds: dict[str, int] | None = None,
    port: int = 8787,
    env: Mapping[str, str] | None = None,
    no_color: bool = False,
) -> DoctorReport:
    """Run the requested checks and return an aggregated ``DoctorReport``.

    *data_dir* is the dani state directory (read-only). *check_names* selects
    a subset of registered checks; ``None`` runs every check. *thresholds*
    overrides default thresholds for ``disk_usage`` / ``backup_files`` /
    ``stuck_*`` / ``process_sprawl`` (see ``ALLOWED_THRESHOLD_KEYS``).

    Never mutates state — even on internal failure each check is captured as
    a ``CheckResult(status=FAIL, error=...)`` rather than re-raising.
    """

    started = _utc_now()
    effective_env: Mapping[str, str] = env if env is not None else os.environ

    config_parsed, config_parse_error = read_only_config(data_dir)
    snapshot, snapshot_errors = read_only_snapshot(data_dir)

    ctx = CheckContext(
        data_dir=data_dir,
        config_parsed=config_parsed,
        config_parse_error=config_parse_error,
        snapshot=snapshot,
        snapshot_errors=snapshot_errors,
        env=effective_env,
        now_utc=started,
        timeout_seconds=timeout,
        verbose=verbose,
        thresholds=thresholds or {},
        port=port,
        no_color=no_color,
    )

    selected: list[tuple[str, Callable[[CheckContext], CheckResult]]] = []
    if check_names is None:
        selected = list(CHECK_REGISTRY.items())
    else:
        for name in check_names:
            if name not in CHECK_REGISTRY:
                # Unknown check name surfaces as a FAIL result so users see it.
                continue
            selected.append((name, CHECK_REGISTRY[name]))

    # If user requested unknown check names, add explicit FAIL results.
    requested_set = set(check_names) if check_names is not None else set()
    known_set = set(CHECK_REGISTRY.keys())
    unknown = sorted(requested_set - known_set)

    results: list[CheckResult] = []
    for name, func in selected:
        t0 = time.monotonic()
        try:
            result = func(ctx)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            results.append(
                CheckResult(
                    name=name,
                    status=CheckStatus.FAIL,
                    summary=f"check raised {type(exc).__name__}",
                    details={},
                    duration_ms=elapsed_ms,
                    error=cap_str(exc, limit=400),
                )
            )
            continue
        if not isinstance(result, CheckResult):
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            results.append(
                CheckResult(
                    name=name,
                    status=CheckStatus.FAIL,
                    summary="check returned non-CheckResult value",
                    details={"returned_type": type(result).__name__},
                    duration_ms=elapsed_ms,
                    error="invalid check return type",
                )
            )
            continue
        if result.duration_ms == 0:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            result = CheckResult(
                name=result.name or name,
                status=result.status,
                summary=result.summary,
                details=result.details,
                duration_ms=elapsed_ms,
                error=result.error,
            )
        results.append(result)

    for name in unknown:
        results.append(
            CheckResult(
                name=name,
                status=CheckStatus.FAIL,
                summary=f"unknown check: {name!r}",
                details={"known_checks": sorted(known_set)},
                duration_ms=0,
                error="unknown check name",
            )
        )

    finished = _utc_now()
    overall = compute_overall(results)
    return DoctorReport(
        schema_version=1,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        data_dir=str(data_dir),
        host=_host_info(port),
        overall_status=overall,
        summary=_summary_counts(results),
        results=results,
    )


# ============================================================
# 9. Formatters
# ============================================================


_ANSI_RESET = "\x1b[0m"
_ANSI_STATUS_COLOR: dict[CheckStatus, str] = {
    CheckStatus.OK: "\x1b[32m",
    CheckStatus.WARN: "\x1b[33m",
    CheckStatus.FAIL: "\x1b[31m",
    CheckStatus.SKIP: "\x1b[2m",
}

_STATUS_LABEL: dict[CheckStatus, str] = {
    CheckStatus.OK: "[ OK ]",
    CheckStatus.WARN: "[WARN]",
    CheckStatus.FAIL: "[FAIL]",
    CheckStatus.SKIP: "[SKIP]",
}


def _result_to_json_dict(result: CheckResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "status": result.status.value,
        "summary": result.summary,
        "details": result.details,
        "duration_ms": result.duration_ms,
        "error": result.error,
    }


def format_json(report: DoctorReport) -> str:
    payload: dict[str, Any] = {
        "schema_version": report.schema_version,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "data_dir": report.data_dir,
        "host": report.host,
        "overall_status": report.overall_status.value,
        "summary": report.summary,
        "results": [_result_to_json_dict(r) for r in report.results],
    }
    return json.dumps(payload, indent=2, default=str)


def format_text(report: DoctorReport, *, verbose: bool, use_color: bool) -> str:
    lines: list[str] = []
    host = report.host
    header = (
        f"dani doctor — schema {report.schema_version} — "
        f"host: {host.get('platform', '?')} python {host.get('python_version', '?')} "
        f"dani {host.get('dani_version', '?')} — data_dir: {report.data_dir}"
    )
    lines.append(header)
    lines.append("")
    for result in report.results:
        label = _STATUS_LABEL[result.status]
        if use_color:
            color = _ANSI_STATUS_COLOR[result.status]
            label = f"{color}{label}{_ANSI_RESET}"
        lines.append(f"{label} {result.name} — {result.summary} ({result.duration_ms}ms)")
        if result.error:
            lines.append(f"        error: {cap_str(result.error, limit=400)}")
        if verbose and result.details:
            detail_text = json.dumps(result.details, indent=2, default=str, sort_keys=True)
            for detail_line in detail_text.splitlines():
                lines.append(f"    {detail_line}")
    lines.append("")
    counts = report.summary or {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    overall_label = _STATUS_LABEL[report.overall_status]
    if use_color:
        overall_label = f"{_ANSI_STATUS_COLOR[report.overall_status]}{overall_label}{_ANSI_RESET}"
    lines.append(
        f"Summary: {counts.get('ok', 0)} ok, {counts.get('warn', 0)} warn, "
        f"{counts.get('fail', 0)} fail, {counts.get('skip', 0)} skip — overall: {overall_label}"
    )
    return "\n".join(lines)


# ============================================================
# 10. CLI argument helpers
# ============================================================


class _ThresholdParseError(ValueError):
    pass


def parse_thresholds(values: list[str]) -> dict[str, int]:
    """Parse repeated ``--threshold KEY=VALUE`` entries.

    Each *value* must be ``KEY=POSITIVE_INTEGER`` with ``KEY`` in
    ``ALLOWED_THRESHOLD_KEYS``. Raises ``_ThresholdParseError`` on any
    parse failure (caller maps to typer.BadParameter).
    """

    parsed: dict[str, int] = {}
    for raw in values or []:
        if "=" not in raw:
            msg = f"invalid --threshold {raw!r}: expected KEY=VALUE"
            raise _ThresholdParseError(msg)
        key, _, val = raw.partition("=")
        key = key.strip()
        val = val.strip()
        if key not in ALLOWED_THRESHOLD_KEYS:
            allowed = ", ".join(sorted(ALLOWED_THRESHOLD_KEYS))
            msg = f"unknown threshold key {key!r}. Allowed: {allowed}"
            raise _ThresholdParseError(msg)
        try:
            num = int(val)
        except ValueError as exc:
            msg = f"threshold {key!r} must be an integer, got {val!r}"
            raise _ThresholdParseError(msg) from exc
        if num < 0:
            msg = f"threshold {key!r} must be non-negative, got {num}"
            raise _ThresholdParseError(msg)
        parsed[key] = num
    return parsed


class _OutputPathError(ValueError):
    pass


def validate_output_path(output: Path | None, data_dir: Path) -> Path | None:
    """Validate ``--output PATH`` is safe to write to.

    Returns the resolved absolute path. Raises ``_OutputPathError`` if the
    path resolves to a location under *data_dir*, points to an existing
    directory, or has a non-existent parent directory.
    """

    if output is None:
        return None
    resolved = output.expanduser().resolve(strict=False)
    data_dir_resolved = data_dir.expanduser().resolve(strict=False)
    if resolved == data_dir_resolved or data_dir_resolved in resolved.parents:
        msg = f"--output {output!s} must NOT be under data_dir {data_dir!s}"
        raise _OutputPathError(msg)
    if resolved.exists() and resolved.is_dir():
        msg = f"--output {output!s} points to an existing directory"
        raise _OutputPathError(msg)
    parent = resolved.parent
    if not parent.exists():
        msg = f"--output parent directory does not exist: {parent}"
        raise _OutputPathError(msg)
    return resolved


def resolve_port(explicit: int | None, env: Mapping[str, str], default: int = 8787) -> int:
    """Resolve the port used by ``server_health`` and the default report header.

    Priority: ``explicit`` (e.g. from ``--port``) → ``env['DANI_PORT']`` →
    *default*. Non-integer env values fall back to *default*.
    """

    if explicit is not None:
        return explicit
    env_value = env.get("DANI_PORT", "")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            return default
    return default
