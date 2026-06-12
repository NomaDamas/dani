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
   Never prints raw ``ps`` argv (agent argv can contain full prompts).
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


# ============================================================
# 11. Built-in checks
# ============================================================


VALID_AGENT_RUNTIMES = frozenset({
    "auto",
    "codex",
    "gajae",
    "gajae-code",
    "gjc",
})
CODEX_FAMILY = frozenset({"auto", "codex"})
GAJAE_FAMILY = frozenset({"auto", "gajae", "gajae-code", "gjc"})


def _resolved_agent_runtime(ctx: CheckContext) -> str:
    env_value = ctx.env.get("DANI_AGENT_RUNTIME")
    if env_value:
        return env_value
    if ctx.config_parsed and "agent_runtime" in ctx.config_parsed:
        return str(ctx.config_parsed["agent_runtime"])
    return "auto"


def _resolved_agent_timeout(ctx: CheckContext) -> float:
    env_value = ctx.env.get("DANI_AGENT_TIMEOUT_SECONDS")
    candidate: Any = env_value
    if not candidate and ctx.config_parsed:
        candidate = ctx.config_parsed.get("agent_timeout_seconds")
    try:
        return float(candidate) if candidate is not None else 3600.0
    except (TypeError, ValueError):
        return 3600.0


@register_check("config_env")
def _check_config_env(ctx: CheckContext) -> CheckResult:
    details: dict[str, Any] = {}
    failures: list[str] = []
    warnings: list[str] = []

    if ctx.config_parse_error:
        failures.append(f"config.json: {ctx.config_parse_error}")
        details["config_parse_error"] = ctx.config_parse_error

    webhook_secret = ctx.env.get("DANI_WEBHOOK_SECRET", "")
    if not webhook_secret:
        failures.append("DANI_WEBHOOK_SECRET is unset")

    token, source = resolve_github_token(ctx.env)
    if token is None:
        failures.append("no GitHub token (DANI_GITHUB_TOKEN/GITHUB_TOKEN/GH_TOKEN/GITHUB_PAT)")

    runtime = _resolved_agent_runtime(ctx)
    if runtime not in VALID_AGENT_RUNTIMES:
        warnings.append(f"unrecognized agent_runtime: {runtime!r}")

    timeout_seconds = _resolved_agent_timeout(ctx)
    if timeout_seconds <= 0:
        warnings.append(f"agent_timeout_seconds is non-positive: {timeout_seconds}")

    details.update({
        "webhook_secret": "<set>" if webhook_secret else "<unset>",
        "github_token_source": source,
        "agent_runtime": runtime,
        "agent_timeout_seconds": timeout_seconds,
        "config_path_exists": (ctx.data_dir / CONFIG_FILE).exists(),
    })

    if failures:
        return CheckResult(
            name="config_env",
            status=CheckStatus.FAIL,
            summary="; ".join(failures),
            details=details,
        )
    if warnings:
        return CheckResult(
            name="config_env",
            status=CheckStatus.WARN,
            summary="; ".join(warnings),
            details=details,
        )
    return CheckResult(
        name="config_env",
        status=CheckStatus.OK,
        summary="environment + config look healthy",
        details=details,
    )


def _probe_binary_version(binary: str, *, timeout_seconds: float) -> str:
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=min(2.0, timeout_seconds),
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"
    output = (result.stdout or result.stderr or "").strip().splitlines()
    return cap_str(output[0], limit=100) if output else "unknown"


def _binary_record(
    name: str, *, required: bool, timeout_seconds: float, severity_when_missing: CheckStatus
) -> dict[str, Any]:
    import shutil

    path = shutil.which(name)
    if path is None:
        return {
            "name": name,
            "path": None,
            "version": None,
            "required": required,
            "found": False,
            "severity": severity_when_missing.value,
        }
    return {
        "name": name,
        "path": path,
        "version": _probe_binary_version(name, timeout_seconds=timeout_seconds),
        "required": required,
        "found": True,
        "severity": CheckStatus.OK.value,
    }


@register_check("binaries")
def _check_binaries(ctx: CheckContext) -> CheckResult:
    runtime = _resolved_agent_runtime(ctx).lower()
    records: list[dict[str, Any]] = []

    records.append(
        _binary_record(
            "git",
            required=True,
            timeout_seconds=ctx.timeout_seconds,
            severity_when_missing=CheckStatus.FAIL,
        )
    )
    records.append(
        _binary_record(
            "gh",
            required=False,
            timeout_seconds=ctx.timeout_seconds,
            severity_when_missing=CheckStatus.WARN,
        )
    )

    if runtime in CODEX_FAMILY:
        records.append(
            _binary_record(
                "codex",
                required=True,
                timeout_seconds=ctx.timeout_seconds,
                severity_when_missing=CheckStatus.FAIL,
            )
        )
    else:
        records.append({
            "name": "codex",
            "found": None,
            "required": False,
            "severity": CheckStatus.SKIP.value,
            "skip_reason": f"agent_runtime={runtime}",
        })

    if runtime in GAJAE_FAMILY:
        records.append(
            _binary_record(
                "gjc",
                required=True,
                timeout_seconds=ctx.timeout_seconds,
                severity_when_missing=CheckStatus.FAIL,
            )
        )
    else:
        records.append({
            "name": "gjc",
            "found": None,
            "required": False,
            "severity": CheckStatus.SKIP.value,
            "skip_reason": f"agent_runtime={runtime}",
        })

    fails = [r["name"] for r in records if r.get("severity") == "fail"]
    warns = [r["name"] for r in records if r.get("severity") == "warn"]

    details = {
        "runtime": runtime,
        "binaries": records,
    }

    if fails:
        return CheckResult(
            name="binaries",
            status=CheckStatus.FAIL,
            summary=f"missing required binary: {', '.join(fails)}",
            details=details,
        )
    if warns:
        return CheckResult(
            name="binaries",
            status=CheckStatus.WARN,
            summary=f"missing advisory binary: {', '.join(warns)}",
            details=details,
        )
    return CheckResult(
        name="binaries",
        status=CheckStatus.OK,
        summary="all required binaries present",
        details=details,
    )


@register_check("storage_files")
def _check_storage_files(ctx: CheckContext) -> CheckResult:
    files_info: list[dict[str, Any]] = []
    fail_files: list[str] = []
    warn_files: list[str] = []

    for key, filename in SNAPSHOT_FILES.items():
        path = ctx.data_dir / filename
        size = path.stat().st_size if path.exists() else 0
        error = ctx.snapshot_errors.get(key)
        record = {
            "name": filename,
            "exists": path.exists(),
            "size_bytes": size,
            "parsed_ok": error is None and ctx.snapshot.get(key) is not None,
            "error": error,
            "required": key in REQUIRED_SNAPSHOT_KEYS,
        }
        files_info.append(record)
        if error and error != "missing":
            if key in REQUIRED_SNAPSHOT_KEYS:
                fail_files.append(filename)
            else:
                warn_files.append(filename)
        if error == "missing" and key in REQUIRED_SNAPSHOT_KEYS:
            warn_files.append(f"{filename}(missing)")

    events_meta = ctx.snapshot.get("events_jsonl_meta", {})
    events_record = {
        "name": EVENTS_JSONL,
        "exists": events_meta.get("exists", False),
        "size_bytes": events_meta.get("size_bytes", 0),
        "line_count": events_meta.get("line_count", 0),
        "first_line_parsed": events_meta.get("first_line_parsed", False),
        "last_line_parsed": events_meta.get("last_line_parsed", False),
        "last_line_invalid": events_meta.get("last_line_invalid", False),
        "error": events_meta.get("error"),
    }
    files_info.append(events_record)
    if events_meta.get("last_line_invalid"):
        warn_files.append(f"{EVENTS_JSONL}(last_line_invalid)")

    details = {"files": files_info}

    if fail_files:
        return CheckResult(
            name="storage_files",
            status=CheckStatus.FAIL,
            summary=f"unparsable required storage files: {', '.join(fail_files)}",
            details=details,
        )
    if warn_files:
        return CheckResult(
            name="storage_files",
            status=CheckStatus.WARN,
            summary=f"storage warnings: {', '.join(warn_files)}",
            details=details,
        )
    return CheckResult(
        name="storage_files",
        status=CheckStatus.OK,
        summary="all storage files exist and parse",
        details=details,
    )


def _git_branch_exists(repo_path: Path, branch: str, timeout_seconds: float) -> bool:
    import subprocess

    cmd = ["git", "-C", str(repo_path), "rev-parse", "--verify", "--end-of-options", branch]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=min(3.0, timeout_seconds),
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def _is_git_worktree(repo_path: Path, timeout_seconds: float) -> bool:
    import subprocess

    cmd = ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=min(3.0, timeout_seconds),
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _inspect_one_repo(repo: dict[str, Any], timeout_seconds: float) -> tuple[dict[str, Any], bool, bool]:
    full_name = repo.get("full_name", "?")
    local_path_str = repo.get("local_path", "")
    enabled = repo.get("enabled", True)
    local_path = Path(local_path_str) if local_path_str else None
    errors: list[str] = []
    is_worktree = False
    main_branch_ok = False
    dev_branch_ok = False
    main_branch = str(repo.get("main_branch", "main"))
    dev_branch = str(repo.get("dev_branch", "dev"))
    fail = False
    warn = False

    if not enabled:
        record = {
            "full_name": full_name,
            "local_path": local_path_str,
            "enabled": False,
            "skipped": True,
        }
        return record, fail, warn

    if local_path is None or not local_path.exists():
        errors.append("local_path missing")
        fail = True
    else:
        is_worktree = _is_git_worktree(local_path, timeout_seconds)
        if not is_worktree:
            errors.append("not a git working tree")
            fail = True
        else:
            main_branch_ok = _git_branch_exists(local_path, main_branch, timeout_seconds)
            dev_branch_ok = _git_branch_exists(local_path, dev_branch, timeout_seconds)
            if not main_branch_ok:
                errors.append(f"missing branch: {main_branch}")
                warn = True
            if not dev_branch_ok:
                errors.append(f"missing branch: {dev_branch}")
                warn = True

    record = {
        "full_name": full_name,
        "local_path": local_path_str,
        "exists": local_path.exists() if local_path else False,
        "is_worktree": is_worktree,
        "main_branch": main_branch,
        "main_branch_ok": main_branch_ok,
        "dev_branch": dev_branch,
        "dev_branch_ok": dev_branch_ok,
        "enabled": enabled,
        "errors": errors,
    }
    return record, fail, warn


@register_check("registered_repos")
def _check_registered_repos(ctx: CheckContext) -> CheckResult:
    registry = ctx.snapshot.get("registry") or {}
    repos = registry.get("repos", []) if isinstance(registry, dict) else []
    if not repos:
        return CheckResult(
            name="registered_repos",
            status=CheckStatus.SKIP,
            summary="no repos registered",
            details={"count": 0},
        )

    repo_records: list[dict[str, Any]] = []
    any_fail = False
    any_warn = False
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        record, fail, warn = _inspect_one_repo(repo, ctx.timeout_seconds)
        repo_records.append(record)
        any_fail = any_fail or fail
        any_warn = any_warn or warn

    details = {"count": len(repos), "repos": repo_records}
    if any_fail:
        return CheckResult(
            name="registered_repos",
            status=CheckStatus.FAIL,
            summary="one or more repos missing or not a git working tree",
            details=details,
        )
    if any_warn:
        return CheckResult(
            name="registered_repos",
            status=CheckStatus.WARN,
            summary="one or more repos missing main/dev branch",
            details=details,
        )
    return CheckResult(
        name="registered_repos",
        status=CheckStatus.OK,
        summary=f"{len(repos)} repo(s) healthy",
        details=details,
    )


@register_check("github_auth")
def _check_github_auth(ctx: CheckContext) -> CheckResult:
    token, source = resolve_github_token(ctx.env)
    if token is None:
        return CheckResult(
            name="github_auth",
            status=CheckStatus.SKIP,
            summary="no GitHub token available",
            details={"source": None},
        )
    try:
        from github import Auth, Github
        from github.GithubException import BadCredentialsException, GithubException
    except ImportError as exc:
        return CheckResult(
            name="github_auth",
            status=CheckStatus.WARN,
            summary=f"PyGithub not importable: {exc}",
            details={"source": source},
        )

    try:
        client = Github(auth=Auth.Token(token), timeout=int(min(ctx.timeout_seconds, 10.0)), per_page=1, retry=None)
        rate_limit = client.get_rate_limit()
        core = getattr(rate_limit, "core", None)
        remaining = getattr(core, "remaining", None) if core else None
        limit = getattr(core, "limit", None) if core else None
        reset = getattr(core, "reset", None) if core else None
        reset_iso = reset.isoformat() if reset is not None else None
    except BadCredentialsException:
        return CheckResult(
            name="github_auth",
            status=CheckStatus.FAIL,
            summary="invalid GitHub token (BadCredentials)",
            details={"source": source},
        )
    except GithubException as exc:
        return CheckResult(
            name="github_auth",
            status=CheckStatus.WARN,
            summary=f"GitHub API error: {cap_str(exc, limit=200)}",
            details={"source": source},
        )
    except Exception as exc:
        return CheckResult(
            name="github_auth",
            status=CheckStatus.WARN,
            summary=f"GitHub API transient error: {cap_str(exc, limit=200)}",
            details={"source": source},
        )

    details = {"source": source, "rate_limit": {"remaining": remaining, "limit": limit, "reset": reset_iso}}
    if remaining is not None and remaining < 100:
        return CheckResult(
            name="github_auth",
            status=CheckStatus.WARN,
            summary=f"rate-limit remaining low: {remaining}/{limit}",
            details=details,
        )
    return CheckResult(
        name="github_auth",
        status=CheckStatus.OK,
        summary=f"token ok (rate-limit {remaining}/{limit})",
        details=details,
    )


def _port_is_listening(port: int) -> bool:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        result = sock.connect_ex(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return result == 0


@register_check("server_health")
def _check_server_health(ctx: CheckContext) -> CheckResult:
    port = ctx.port
    listening = _port_is_listening(port)
    if not listening:
        return CheckResult(
            name="server_health",
            status=CheckStatus.SKIP,
            summary=f"no local server on 127.0.0.1:{port}",
            details={"port": port, "listening": False},
        )

    import time as _time
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/health"
    start = _time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dani-doctor"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=min(2.0, ctx.timeout_seconds)) as resp:  # noqa: S310
            status_code = resp.status
            body_bytes = resp.read(4096)
    except urllib.error.HTTPError as exc:
        return CheckResult(
            name="server_health",
            status=CheckStatus.WARN,
            summary=f"non-200 from {url}: HTTP {exc.code}",
            details={
                "port": port,
                "listening": True,
                "likely_not_dani": True,
                "http_status": exc.code,
                "checked_url": url,
            },
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return CheckResult(
            name="server_health",
            status=CheckStatus.WARN,
            summary=f"server probe failed: {cap_str(exc, limit=200)}",
            details={"port": port, "listening": True, "checked_url": url},
        )
    latency_ms = int((_time.monotonic() - start) * 1000)
    body_text = body_bytes.decode("utf-8", errors="replace")
    try:
        body_parsed = json.loads(body_text)
    except json.JSONDecodeError:
        body_parsed = None
    if status_code == 200 and body_parsed == {"status": "ok"}:
        return CheckResult(
            name="server_health",
            status=CheckStatus.OK,
            summary=f"healthy ({latency_ms}ms)",
            details={
                "port": port,
                "listening": True,
                "http_status": 200,
                "latency_ms": latency_ms,
                "checked_url": url,
            },
        )
    return CheckResult(
        name="server_health",
        status=CheckStatus.WARN,
        summary="200 but body does not match dani /health shape",
        details={
            "port": port,
            "listening": True,
            "likely_not_dani": True,
            "http_status": status_code,
            "body_preview": cap_str(body_text, limit=200),
            "checked_url": url,
            "latency_ms": latency_ms,
        },
    )


_ACTIVE_JOB_STATUSES = frozenset({"queued", "launched", "retrying", "recovering"})


@register_check("stuck_jobs")
def _check_stuck_jobs(ctx: CheckContext) -> CheckResult:
    jobs_payload = ctx.snapshot.get("jobs") or {}
    jobs = jobs_payload.get("jobs", []) if isinstance(jobs_payload, dict) else []
    threshold_s = ctx.thresholds.get("stuck_job_age_seconds")
    if threshold_s is None:
        threshold_s = int(_resolved_agent_timeout(ctx))

    stuck: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("status") not in _ACTIVE_JOB_STATUSES:
            continue
        updated = safe_iso_parse(job.get("updated_at") or job.get("created_at"))
        if updated is None:
            continue
        age_s = (ctx.now_utc - updated).total_seconds()
        if age_s <= threshold_s:
            continue
        stuck.append({
            "job_id": job.get("id"),
            "repo": job.get("repo_full_name"),
            "stage": job.get("stage"),
            "status": job.get("status"),
            "age_seconds": int(age_s),
            "updated_at": job.get("updated_at"),
            "issue_number": job.get("issue_number"),
            "pr_number": job.get("pr_number"),
        })

    capped, overflow = cap_list(stuck, limit=20)
    details = {
        "threshold_seconds": threshold_s,
        "stuck_count": len(stuck),
        "stuck": capped,
        "overflow": overflow,
    }
    if not stuck:
        return CheckResult(
            name="stuck_jobs",
            status=CheckStatus.OK,
            summary="no stuck active jobs",
            details=details,
        )
    return CheckResult(
        name="stuck_jobs",
        status=CheckStatus.WARN,
        summary=f"{len(stuck)} active job(s) older than {threshold_s}s",
        details=details,
    )


_DRIFT_GRACE_SECONDS = 60


def _classify_sessions(
    sessions: list[Any],
    job_by_id: dict[str, dict[str, Any]],
    now_utc: datetime,
    long_running_threshold_s: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    drift: list[dict[str, Any]] = []
    long_running: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if session.get("status") != "launched":
            continue
        updated = safe_iso_parse(session.get("updated_at") or session.get("created_at"))
        age_s = (now_utc - updated).total_seconds() if updated else None
        job_id = session.get("job_id")
        job = job_by_id.get(job_id) if isinstance(job_id, str) else None
        entry = {
            "session_id": session.get("id"),
            "job_id": job_id,
            "repo": session.get("repo_full_name"),
            "stage": session.get("stage"),
            "session_status": session.get("status"),
            "job_status": job.get("status") if job else None,
            "age_seconds": int(age_s) if age_s is not None else None,
        }
        if job is None:
            orphans.append(entry)
            continue
        if job.get("status") in TERMINAL_JOB_STATUSES:
            if age_s is None or age_s >= _DRIFT_GRACE_SECONDS:
                drift.append(entry)
            continue
        if age_s is not None and age_s > long_running_threshold_s:
            long_running.append(entry)
    return drift, long_running, orphans


@register_check("stuck_sessions")
def _check_stuck_sessions(ctx: CheckContext) -> CheckResult:
    jobs_payload = ctx.snapshot.get("jobs") or {}
    sessions_payload = ctx.snapshot.get("sessions") or {}
    jobs = jobs_payload.get("jobs", []) if isinstance(jobs_payload, dict) else []
    sessions = sessions_payload.get("sessions", []) if isinstance(sessions_payload, dict) else []

    job_by_id: dict[str, dict[str, Any]] = {
        j["id"]: j for j in jobs if isinstance(j, dict) and isinstance(j.get("id"), str)
    }

    long_running_threshold_s = max(int(_resolved_agent_timeout(ctx)), 86400)
    drift, long_running, orphans = _classify_sessions(sessions, job_by_id, ctx.now_utc, long_running_threshold_s)

    if drift:
        time.sleep(0.1)
        snap2, _errs2 = read_only_snapshot(ctx.data_dir, retry_delay_s=0.0)
        jobs_payload2 = snap2.get("jobs") or {}
        sessions_payload2 = snap2.get("sessions") or {}
        jobs2 = jobs_payload2.get("jobs", []) if isinstance(jobs_payload2, dict) else []
        sessions2 = sessions_payload2.get("sessions", []) if isinstance(sessions_payload2, dict) else []
        job_by_id2: dict[str, dict[str, Any]] = {
            j["id"]: j for j in jobs2 if isinstance(j, dict) and isinstance(j.get("id"), str)
        }
        drift2, _, _ = _classify_sessions(
            sessions2, job_by_id2, datetime.now(tz=timezone.utc), long_running_threshold_s
        )
        confirmed_ids = {d["session_id"] for d in drift2}
        drift = [d for d in drift if d["session_id"] in confirmed_ids]

    drift_capped, drift_overflow = cap_list(drift, limit=50)
    lr_capped, lr_overflow = cap_list(long_running, limit=50)
    orph_capped, orph_overflow = cap_list(orphans, limit=50)
    details: dict[str, Any] = {
        "drift": drift_capped,
        "drift_overflow": drift_overflow,
        "long_running": lr_capped,
        "long_running_overflow": lr_overflow,
        "orphans": orph_capped,
        "orphans_overflow": orph_overflow,
        "long_running_threshold_seconds": long_running_threshold_s,
        "recommendation": (
            "Review listed session_id/job_id pairs. If confirmed stale, stop dani serve "
            "before any manual state edit, or wait for a future repair command."
        ),
    }

    if drift:
        return CheckResult(
            name="stuck_sessions",
            status=CheckStatus.FAIL,
            summary=f"{len(drift)} session(s) launched while job is terminal (drift)",
            details=details,
        )
    if long_running or orphans:
        return CheckResult(
            name="stuck_sessions",
            status=CheckStatus.WARN,
            summary=(f"{len(long_running)} long-running, {len(orphans)} orphan launched session(s)"),
            details=details,
        )
    return CheckResult(
        name="stuck_sessions",
        status=CheckStatus.OK,
        summary="no stuck launched sessions",
        details=details,
    )


_DEFAULT_DISK_THRESHOLDS: dict[str, int] = {
    "jobs_bytes_warn": 50 * 1024 * 1024,
    "jobs_bytes_fail": 200 * 1024 * 1024,
    "sessions_bytes_warn": 20 * 1024 * 1024,
    "sessions_bytes_fail": 100 * 1024 * 1024,
    "events_bytes_warn": 100 * 1024 * 1024,
    "runs_bytes_warn": 5 * 1024 * 1024 * 1024,
    "runs_bytes_fail": 20 * 1024 * 1024 * 1024,
}


def _walk_runs_size(runs_dir: Path, deadline_s: float) -> tuple[int, bool]:
    if not runs_dir.exists():
        return 0, False
    total = 0
    truncated = False
    start = time.monotonic()
    for root, _dirs, files in os.walk(runs_dir):
        if (time.monotonic() - start) > deadline_s:
            truncated = True
            break
        for filename in files:
            try:
                total += os.path.getsize(os.path.join(root, filename))
            except OSError:
                continue
    return total, truncated


def _verdict_for_size(size: int, warn: int, fail: int) -> str:
    if size > fail:
        return "fail"
    if size > warn:
        return "warn"
    return "ok"


@register_check("disk_usage")
def _check_disk_usage(ctx: CheckContext) -> CheckResult:
    thresholds = {**_DEFAULT_DISK_THRESHOLDS, **ctx.thresholds}
    records: list[dict[str, Any]] = []
    overall_fail = False
    overall_warn = False

    file_specs: list[tuple[str, str, int, int]] = [
        ("jobs.json", "jobs", thresholds["jobs_bytes_warn"], thresholds["jobs_bytes_fail"]),
        ("sessions.json", "sessions", thresholds["sessions_bytes_warn"], thresholds["sessions_bytes_fail"]),
        ("events.jsonl", "events", thresholds["events_bytes_warn"], thresholds["events_bytes_warn"]),
        ("processed-events.json", "processed_events", thresholds["jobs_bytes_warn"], thresholds["jobs_bytes_fail"]),
        ("terminal-targets.json", "terminal_targets", thresholds["jobs_bytes_warn"], thresholds["jobs_bytes_fail"]),
    ]
    for filename, key, warn, fail in file_specs:
        path = ctx.data_dir / filename
        size = path.stat().st_size if path.exists() else 0
        verdict = _verdict_for_size(size, warn, fail)
        records.append({"name": filename, "key": key, "bytes": size, "warn": warn, "fail": fail, "verdict": verdict})
        if verdict == "fail":
            overall_fail = True
        elif verdict == "warn":
            overall_warn = True

    runs_deadline = min(30.0, max(ctx.timeout_seconds * 6.0, 5.0))
    runs_dir = ctx.data_dir / "runs"
    runs_size, truncated = _walk_runs_size(runs_dir, runs_deadline)
    runs_verdict = _verdict_for_size(runs_size, thresholds["runs_bytes_warn"], thresholds["runs_bytes_fail"])
    if truncated:
        runs_verdict = "warn" if runs_verdict == "ok" else runs_verdict
        overall_warn = True
    if runs_verdict == "fail":
        overall_fail = True
    elif runs_verdict == "warn":
        overall_warn = True
    records.append({
        "name": "runs/",
        "key": "runs",
        "bytes": runs_size,
        "warn": thresholds["runs_bytes_warn"],
        "fail": thresholds["runs_bytes_fail"],
        "verdict": runs_verdict,
        "truncated": truncated,
        "deadline_seconds": runs_deadline,
    })

    details = {"records": records, "thresholds": thresholds}
    if overall_fail:
        return CheckResult(
            name="disk_usage",
            status=CheckStatus.FAIL,
            summary="one or more storage targets exceed fail threshold",
            details=details,
        )
    if overall_warn:
        return CheckResult(
            name="disk_usage",
            status=CheckStatus.WARN,
            summary="one or more storage targets exceed warn threshold",
            details=details,
        )
    return CheckResult(
        name="disk_usage",
        status=CheckStatus.OK,
        summary="storage usage healthy",
        details=details,
    )


_DEFAULT_BACKUP_THRESHOLDS: dict[str, int] = {
    "backup_count_warn": 3,
    "backup_age_days_warn": 14,
    "backup_bytes_warn": 20 * 1024 * 1024,
}


@register_check("backup_files")
def _check_backup_files(ctx: CheckContext) -> CheckResult:
    thresholds = {**_DEFAULT_BACKUP_THRESHOLDS, **ctx.thresholds}
    candidates = sorted(ctx.data_dir.glob("*.bak.*"))
    items: list[dict[str, Any]] = []
    total_bytes = 0
    oldest_mtime: float | None = None
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        total_bytes += stat.st_size
        if oldest_mtime is None or stat.st_mtime < oldest_mtime:
            oldest_mtime = stat.st_mtime
        items.append({
            "name": path.name,
            "size_bytes": stat.st_size,
            "mtime_iso": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "age_days": (ctx.now_utc.timestamp() - stat.st_mtime) / 86400.0,
        })

    oldest_age_days = (ctx.now_utc.timestamp() - oldest_mtime) / 86400.0 if oldest_mtime is not None else 0.0
    capped, overflow = cap_list(items, limit=20)
    details = {
        "count": len(items),
        "total_bytes": total_bytes,
        "oldest_age_days": oldest_age_days,
        "files": capped,
        "overflow": overflow,
        "thresholds": thresholds,
    }
    reasons: list[str] = []
    if len(items) > thresholds["backup_count_warn"]:
        reasons.append(f"count {len(items)} > {thresholds['backup_count_warn']}")
    if oldest_age_days > thresholds["backup_age_days_warn"]:
        reasons.append(f"oldest {oldest_age_days:.1f}d > {thresholds['backup_age_days_warn']}d")
    if total_bytes > thresholds["backup_bytes_warn"]:
        reasons.append(f"total {total_bytes}B > {thresholds['backup_bytes_warn']}B")
    if reasons:
        return CheckResult(
            name="backup_files",
            status=CheckStatus.WARN,
            summary="; ".join(reasons),
            details=details,
        )
    return CheckResult(
        name="backup_files",
        status=CheckStatus.OK,
        summary="no concerning backup accumulation",
        details=details,
    )


def _ps_run(args: list[str], *, timeout_s: float) -> tuple[int, list[str]]:
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603
            args, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return -1, []
    if result.returncode != 0:
        return result.returncode, []
    return 0, result.stdout.splitlines()


def _classify_ps_command(command: str) -> str | None:
    if "codex exec" in command:
        return "codex_exec"
    if "gjc --print" in command:
        return "gjc_print"
    return None


@register_check("process_sprawl")
def _check_process_sprawl(ctx: CheckContext) -> CheckResult:
    threshold = ctx.thresholds.get("process_sprawl_count_warn", 20)
    timeout_s = min(3.0, ctx.timeout_seconds)
    rc, lines = _ps_run(["ps", "-axo", "pid=,ppid=,etime="], timeout_s=timeout_s)
    if rc != 0:
        return CheckResult(
            name="process_sprawl",
            status=CheckStatus.SKIP,
            summary="ps unavailable or failed",
            details={"reason": "ps_failed"},
        )

    pid_records: list[tuple[str, str, str]] = []
    for line in lines:
        parts = line.split(None, 2)
        if len(parts) >= 3:
            pid_records.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))

    classifier_counts: dict[str, int] = {"codex_exec": 0, "gjc_print": 0}
    samples: list[dict[str, Any]] = []
    sample_cap = 10

    for pid, ppid, etime in pid_records:
        if not pid.isdigit():
            continue
        rc2, cmd_lines = _ps_run(["ps", "-p", pid, "-o", "command="], timeout_s=min(1.0, timeout_s))
        if rc2 != 0 or not cmd_lines:
            continue
        classifier = _classify_ps_command(cmd_lines[0])
        if classifier is None:
            continue
        classifier_counts[classifier] = classifier_counts.get(classifier, 0) + 1
        if len(samples) < sample_cap:
            samples.append({"pid": pid, "ppid": ppid, "etime": etime, "classifier": classifier})

    total = sum(classifier_counts.values())
    details = {
        "counts": classifier_counts,
        "sample": samples,
        "threshold": threshold,
    }
    if total > threshold:
        return CheckResult(
            name="process_sprawl",
            status=CheckStatus.WARN,
            summary=f"{total} agent process(es) running (threshold {threshold})",
            details=details,
        )
    return CheckResult(
        name="process_sprawl",
        status=CheckStatus.OK,
        summary=f"{total} agent process(es) running",
        details=details,
    )
