from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dani.cli import app
from dani.doctor import (
    ALLOWED_THRESHOLD_KEYS,
    CHECK_REGISTRY,
    CheckContext,
    CheckResult,
    CheckStatus,
    DoctorReport,
    _OutputPathError,
    _ThresholdParseError,
    cap_list,
    cap_str,
    compute_overall,
    format_json,
    format_text,
    parse_thresholds,
    read_only_config,
    read_only_snapshot,
    redact_secrets,
    register_check,
    resolve_github_token,
    resolve_port,
    run_doctor,
    safe_iso_parse,
    validate_output_path,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = dict(CHECK_REGISTRY)
    yield
    CHECK_REGISTRY.clear()
    CHECK_REGISTRY.update(saved)


@pytest.fixture
def populated_data_dir(tmp_path: Path) -> Path:
    (tmp_path / "registry.json").write_text(json.dumps({"repos": []}), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    (tmp_path / "sessions.json").write_text(json.dumps({"sessions": []}), encoding="utf-8")
    (tmp_path / "processed-events.json").write_text(json.dumps({"keys": []}), encoding="utf-8")
    (tmp_path / "terminal-targets.json").write_text(json.dumps({"prs": [], "issues": []}), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"id": "1"}) + "\n" + json.dumps({"id": "2"}) + "\n", encoding="utf-8"
    )
    return tmp_path


def test_registry_registers_check():
    @register_check("scaffold_demo_ok")
    def _check(_ctx: CheckContext) -> CheckResult:
        return CheckResult(name="scaffold_demo_ok", status=CheckStatus.OK, summary="x")

    assert "scaffold_demo_ok" in CHECK_REGISTRY


def test_registry_duplicate_name_raises():
    @register_check("dup")
    def _a(_ctx: CheckContext) -> CheckResult:
        return CheckResult(name="dup", status=CheckStatus.OK, summary="")

    with pytest.raises(ValueError, match="duplicate check registration"):

        @register_check("dup")
        def _b(_ctx: CheckContext) -> CheckResult:
            return CheckResult(name="dup", status=CheckStatus.OK, summary="")


def test_run_doctor_empty_returns_ok(tmp_path: Path):
    report = run_doctor(tmp_path, check_names=[])
    assert isinstance(report, DoctorReport)
    assert report.overall_status == CheckStatus.OK
    assert report.results == []
    assert report.summary == {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    assert report.schema_version == 1


def test_run_doctor_captures_check_exception_as_fail(tmp_path: Path):
    @register_check("boom")
    def _boom(_ctx: CheckContext) -> CheckResult:
        raise RuntimeError("kaboom")

    report = run_doctor(tmp_path, check_names=["boom"])
    assert report.overall_status == CheckStatus.FAIL
    assert len(report.results) == 1
    assert report.results[0].status == CheckStatus.FAIL
    assert "RuntimeError" in report.results[0].summary
    assert report.results[0].error and "kaboom" in report.results[0].error


def test_run_doctor_records_unknown_check_as_fail(tmp_path: Path):
    report = run_doctor(tmp_path, check_names=["does_not_exist"])
    assert report.overall_status == CheckStatus.FAIL
    assert len(report.results) == 1
    assert report.results[0].name == "does_not_exist"
    assert "unknown" in report.results[0].summary


def test_redact_secrets_known_keys():
    env = {
        "DANI_WEBHOOK_SECRET": "very-secret-1",
        "DANI_GITHUB_TOKEN": "ghp_xxx",
        "GITHUB_TOKEN": "",
        "PATH": "/usr/bin",
    }
    redacted = redact_secrets(env)
    assert redacted["DANI_WEBHOOK_SECRET"] == "<set>"
    assert redacted["DANI_GITHUB_TOKEN"] == "<set>"
    assert redacted["GITHUB_TOKEN"] == "<unset>"
    assert redacted["GH_TOKEN"] == "<unset>"
    assert redacted["GITHUB_PAT"] == "<unset>"
    assert "very-secret-1" not in json.dumps(redacted)
    assert "ghp_xxx" not in json.dumps(redacted)
    assert "PATH" not in redacted


def test_resolve_github_token_priority():
    env = {"DANI_GITHUB_TOKEN": "dani-tok", "GITHUB_TOKEN": "gh-tok", "GH_TOKEN": "ghc-tok"}
    token, source = resolve_github_token(env)
    assert (token, source) == ("dani-tok", "DANI_GITHUB_TOKEN")

    token2, source2 = resolve_github_token({"GITHUB_TOKEN": "gh-tok", "GH_TOKEN": "ghc-tok"})
    assert (token2, source2) == ("gh-tok", "GITHUB_TOKEN")

    token3, source3 = resolve_github_token({"GH_TOKEN": "ghc-tok"})
    assert (token3, source3) == ("ghc-tok", "GH_TOKEN")

    token4, source4 = resolve_github_token({"GITHUB_PAT": "pat-tok"})
    assert (token4, source4) == ("pat-tok", "GITHUB_PAT")

    assert resolve_github_token({}) == (None, None)
    assert resolve_github_token({"DANI_GITHUB_TOKEN": ""}) == (None, None)


def test_read_only_snapshot_happy_path(populated_data_dir: Path):
    snap, errors = read_only_snapshot(populated_data_dir)
    assert errors == {}
    assert snap["registry"] == {"repos": []}
    assert snap["jobs"] == {"jobs": []}
    assert snap["sessions"] == {"sessions": []}
    assert snap["events_jsonl_meta"]["line_count"] == 2
    assert snap["events_jsonl_meta"]["first_line_parsed"] is True
    assert snap["events_jsonl_meta"]["last_line_parsed"] is True


def test_read_only_snapshot_does_not_create_files(tmp_path: Path):
    pre = sorted(p.name for p in tmp_path.iterdir())
    snap, errors = read_only_snapshot(tmp_path)
    post = sorted(p.name for p in tmp_path.iterdir())
    assert pre == post
    assert pre == []
    assert snap["registry"] is None
    assert errors["registry"] == "missing"


def test_read_only_snapshot_records_persistent_parse_error(tmp_path: Path):
    (tmp_path / "jobs.json").write_text("{not json", encoding="utf-8")
    snap, errors = read_only_snapshot(tmp_path, retry_delay_s=0.0)
    assert snap["jobs"] is None
    assert "JSONDecodeError" in errors["jobs"]


def test_read_only_snapshot_retries_jsondecodeerror_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "jobs.json"
    path.write_text("{not json", encoding="utf-8")
    real_read = Path.read_text

    call_count = {"n": 0}

    def flaky(self: Path, encoding: str = "utf-8") -> str:
        if self == path:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "{not json"
            return json.dumps({"jobs": ["fixed"]})
        return real_read(self, encoding=encoding)

    monkeypatch.setattr(Path, "read_text", flaky)
    snap, errors = read_only_snapshot(tmp_path, retry_delay_s=0.0)
    assert snap["jobs"] == {"jobs": ["fixed"]}
    assert "jobs" not in errors
    assert call_count["n"] == 2


def test_read_only_snapshot_events_jsonl_last_line_invalid(tmp_path: Path):
    (tmp_path / "events.jsonl").write_text(json.dumps({"a": 1}) + "\n" + "{not-json\n", encoding="utf-8")
    snap, _errors = read_only_snapshot(tmp_path, retry_delay_s=0.0)
    meta = snap["events_jsonl_meta"]
    assert meta["first_line_parsed"] is True
    assert meta["last_line_parsed"] is False
    assert meta["last_line_invalid"] is True
    assert meta["line_count"] == 2


def test_read_only_config_missing_returns_none(tmp_path: Path):
    parsed, error = read_only_config(tmp_path)
    assert parsed is None
    assert error is None


def test_read_only_config_invalid_json(tmp_path: Path):
    (tmp_path / "config.json").write_text("not-json", encoding="utf-8")
    parsed, error = read_only_config(tmp_path)
    assert parsed is None
    assert "JSONDecodeError" in (error or "")


def test_read_only_config_non_object(tmp_path: Path):
    (tmp_path / "config.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    parsed, error = read_only_config(tmp_path)
    assert parsed is None
    assert "object" in (error or "")


def test_read_only_config_happy(tmp_path: Path):
    (tmp_path / "config.json").write_text(
        json.dumps({"agent_runtime": "omx", "agent_timeout_seconds": 1800}), encoding="utf-8"
    )
    parsed, error = read_only_config(tmp_path)
    assert parsed == {"agent_runtime": "omx", "agent_timeout_seconds": 1800}
    assert error is None


def test_compute_overall_severity():
    def _r(status: CheckStatus) -> CheckResult:
        return CheckResult(name="n", status=status, summary="")

    assert compute_overall([]) == CheckStatus.OK
    assert compute_overall([_r(CheckStatus.OK), _r(CheckStatus.OK)]) == CheckStatus.OK
    assert compute_overall([_r(CheckStatus.SKIP), _r(CheckStatus.SKIP)]) == CheckStatus.SKIP
    assert compute_overall([_r(CheckStatus.OK), _r(CheckStatus.SKIP)]) == CheckStatus.OK
    assert compute_overall([_r(CheckStatus.OK), _r(CheckStatus.WARN)]) == CheckStatus.WARN
    assert compute_overall([_r(CheckStatus.OK), _r(CheckStatus.WARN), _r(CheckStatus.FAIL)]) == CheckStatus.FAIL
    assert compute_overall([_r(CheckStatus.SKIP), _r(CheckStatus.FAIL)]) == CheckStatus.FAIL


def test_cap_list_and_cap_str():
    items = list(range(50))
    capped, overflow = cap_list(items, limit=10)
    assert capped == list(range(10))
    assert overflow == 40
    assert cap_list([], limit=5) == ([], 0)
    assert cap_list([1, 2], limit=5) == ([1, 2], 0)

    assert cap_str("hello", limit=10) == "hello"
    truncated = cap_str("x" * 50, limit=10)
    assert len(truncated) == 10
    assert truncated.endswith("…")


def test_safe_iso_parse_variants():
    assert safe_iso_parse("") is None
    assert safe_iso_parse(None) is None
    assert safe_iso_parse("not-a-date") is None
    parsed = safe_iso_parse("2026-05-15T09:04:06+00:00")
    assert parsed is not None and parsed.year == 2026
    parsed_z = safe_iso_parse("2026-05-15T09:04:06Z")
    assert parsed_z is not None and parsed_z.tzinfo is not None


def test_format_json_round_trip(tmp_path: Path):
    @register_check("demo")
    def _demo(_ctx: CheckContext) -> CheckResult:
        return CheckResult(
            name="demo",
            status=CheckStatus.OK,
            summary="all good",
            details={"value": 42},
            duration_ms=5,
        )

    report = run_doctor(tmp_path, check_names=["demo"])
    rendered = format_json(report)
    parsed: dict[str, Any] = json.loads(rendered)
    assert parsed["schema_version"] == 1
    assert parsed["overall_status"] == "ok"
    assert parsed["summary"] == {"ok": 1, "warn": 0, "fail": 0, "skip": 0}
    assert parsed["results"][0]["name"] == "demo"
    assert parsed["results"][0]["status"] == "ok"
    assert parsed["results"][0]["details"] == {"value": 42}


def test_format_text_no_color_has_no_ansi(tmp_path: Path):
    @register_check("demo")
    def _demo(_ctx: CheckContext) -> CheckResult:
        return CheckResult(name="demo", status=CheckStatus.WARN, summary="watch out")

    report = run_doctor(tmp_path, check_names=["demo"])
    rendered = format_text(report, verbose=False, use_color=False)
    assert "\x1b" not in rendered
    assert "[WARN] demo" in rendered
    assert "watch out" in rendered


def test_format_text_redaction_does_not_leak_secret_value(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DANI_GITHUB_TOKEN", "ghp_ULTRASECRET_XYZ_999")

    @register_check("demo")
    def _demo(ctx: CheckContext) -> CheckResult:
        return CheckResult(
            name="demo",
            status=CheckStatus.OK,
            summary="env",
            details={"env": redact_secrets(ctx.env)},
        )

    report = run_doctor(tmp_path, check_names=["demo"])
    text = format_text(report, verbose=True, use_color=False)
    j = format_json(report)
    assert "ghp_ULTRASECRET_XYZ_999" not in text
    assert "ghp_ULTRASECRET_XYZ_999" not in j


def test_parse_thresholds_accepts_known_keys():
    parsed = parse_thresholds(["runs_bytes_warn=5000000000", "stuck_job_age_seconds=600"])
    assert parsed == {"runs_bytes_warn": 5000000000, "stuck_job_age_seconds": 600}


def test_parse_thresholds_rejects_unknown_key():
    with pytest.raises(_ThresholdParseError):
        parse_thresholds(["unknown_key=1"])


def test_parse_thresholds_rejects_non_integer():
    with pytest.raises(_ThresholdParseError):
        parse_thresholds(["runs_bytes_warn=abc"])


def test_parse_thresholds_rejects_missing_equals():
    with pytest.raises(_ThresholdParseError):
        parse_thresholds(["runs_bytes_warn"])


def test_parse_thresholds_rejects_negative():
    with pytest.raises(_ThresholdParseError):
        parse_thresholds(["runs_bytes_warn=-1"])


def test_validate_output_path_rejects_under_data_dir(tmp_path: Path):
    output = tmp_path / "report.json"
    with pytest.raises(_OutputPathError):
        validate_output_path(output, tmp_path)


def test_validate_output_path_rejects_existing_dir(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = tmp_path / "subdir"
    target.mkdir()
    with pytest.raises(_OutputPathError):
        validate_output_path(target, data_dir)


def test_validate_output_path_rejects_missing_parent(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    missing_parent = tmp_path / "nope" / "report.json"
    with pytest.raises(_OutputPathError):
        validate_output_path(missing_parent, data_dir)


def test_validate_output_path_accepts_sibling(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out = tmp_path / "report.json"
    resolved = validate_output_path(out, data_dir)
    assert resolved is not None and resolved == out.resolve(strict=False)


def test_resolve_port_precedence():
    assert resolve_port(9000, {"DANI_PORT": "7000"}) == 9000
    assert resolve_port(None, {"DANI_PORT": "7000"}) == 7000
    assert resolve_port(None, {}) == 8787
    assert resolve_port(None, {"DANI_PORT": "not-a-number"}) == 8787


def test_cli_doctor_help_works():
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    # Typer renders --help with Rich panel borders in CI; strip ANSI + whitespace
    # to make assertions resilient against terminal-width-driven line wrapping.
    import re

    stripped = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    flat = "".join(stripped.split())
    assert "doctor" in stripped.lower()
    assert "--data-dir" in flat or "--data-dirPATH" in flat
    assert "--json" in flat
    assert "--strict" in flat


def test_cli_doctor_rejects_output_under_data_dir(tmp_path: Path):
    runner = CliRunner()
    out = tmp_path / "r.json"
    result = runner.invoke(app, ["doctor", "--data-dir", str(tmp_path), "--output", str(out)])
    assert result.exit_code != 0


def test_cli_doctor_rejects_unknown_threshold(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--data-dir", str(tmp_path), "--threshold", "bogus=1"])
    assert result.exit_code != 0


def test_cli_doctor_empty_data_dir_runs_clean(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["doctor", "--data-dir", str(tmp_path), "--json", "--check", "process_sprawl"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["overall_status"] in {"ok", "warn", "skip"}


def test_allowed_threshold_keys_is_frozenset():
    assert isinstance(ALLOWED_THRESHOLD_KEYS, frozenset)
    assert "runs_bytes_warn" in ALLOWED_THRESHOLD_KEYS
    assert "unknown_random_key" not in ALLOWED_THRESHOLD_KEYS


# ============================================================
# W5: per-check tests
# ============================================================


from datetime import datetime, timedelta, timezone  # noqa: E402

from dani.doctor import (  # noqa: E402
    _check_backup_files,
    _check_binaries,
    _check_config_env,
    _check_disk_usage,
    _check_github_auth,
    _check_process_sprawl,
    _check_registered_repos,
    _check_server_health,
    _check_storage_files,
    _check_stuck_jobs,
    _check_stuck_sessions,
)


def _make_ctx(
    data_dir: Path,
    *,
    env: dict[str, str] | None = None,
    snapshot: dict[str, Any] | None = None,
    snapshot_errors: dict[str, str] | None = None,
    config_parsed: dict[str, Any] | None = None,
    config_parse_error: str | None = None,
    now_utc: datetime | None = None,
    thresholds: dict[str, int] | None = None,
    port: int = 8787,
    timeout_seconds: float = 5.0,
    verbose: bool = False,
) -> CheckContext:
    if snapshot is None:
        snapshot = {
            "registry": {"repos": []},
            "jobs": {"jobs": []},
            "sessions": {"sessions": []},
            "processed_events": {"keys": []},
            "terminal_targets": {"prs": [], "issues": []},
            "events_jsonl_meta": {
                "exists": False,
                "size_bytes": 0,
                "line_count": 0,
                "first_line_parsed": False,
                "last_line_parsed": False,
                "last_line_invalid": False,
                "error": "missing",
            },
        }
    return CheckContext(
        data_dir=data_dir,
        config_parsed=config_parsed,
        config_parse_error=config_parse_error,
        snapshot=snapshot,
        snapshot_errors=snapshot_errors or {},
        env=env or {},
        now_utc=now_utc or datetime.now(tz=timezone.utc),
        timeout_seconds=timeout_seconds,
        verbose=verbose,
        thresholds=thresholds or {},
        port=port,
        no_color=True,
    )


def test_config_env_fail_when_secret_and_token_missing(tmp_path: Path):
    ctx = _make_ctx(tmp_path, env={})
    result = _check_config_env(ctx)
    assert result.status == CheckStatus.FAIL
    assert "DANI_WEBHOOK_SECRET" in result.summary
    assert "GitHub token" in result.summary


def test_config_env_ok_when_secret_and_token_set(tmp_path: Path):
    ctx = _make_ctx(
        tmp_path,
        env={"DANI_WEBHOOK_SECRET": "x", "DANI_GITHUB_TOKEN": "y"},
    )
    result = _check_config_env(ctx)
    assert result.status == CheckStatus.OK
    assert result.details["github_token_source"] == "DANI_GITHUB_TOKEN"


def test_config_env_warn_on_bad_runtime(tmp_path: Path):
    ctx = _make_ctx(
        tmp_path,
        env={
            "DANI_WEBHOOK_SECRET": "x",
            "DANI_GITHUB_TOKEN": "y",
            "DANI_AGENT_RUNTIME": "made-up",
        },
    )
    result = _check_config_env(ctx)
    assert result.status == CheckStatus.WARN
    assert "made-up" in result.summary


def test_config_env_fail_on_config_parse_error(tmp_path: Path):
    ctx = _make_ctx(
        tmp_path,
        env={"DANI_WEBHOOK_SECRET": "x", "DANI_GITHUB_TOKEN": "y"},
        config_parse_error="JSONDecodeError: x",
    )
    result = _check_config_env(ctx)
    assert result.status == CheckStatus.FAIL
    assert "JSONDecodeError" in result.details["config_parse_error"]


def test_binaries_fail_when_git_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    ctx = _make_ctx(tmp_path, env={"DANI_AGENT_RUNTIME": "omx"})
    result = _check_binaries(ctx)
    assert result.status == CheckStatus.FAIL
    assert "git" in result.summary


def test_binaries_ok_omx_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_paths = {
        "git": "/usr/bin/git",
        "gh": "/usr/bin/gh",
        "omx": "/usr/bin/omx",
        "codex": "/usr/bin/codex",
    }
    monkeypatch.setattr("shutil.which", lambda name: fake_paths.get(name))
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")
    ctx = _make_ctx(tmp_path, env={"DANI_AGENT_RUNTIME": "omx"})
    result = _check_binaries(ctx)
    assert result.status == CheckStatus.OK


def test_binaries_omo_external_server_skips_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_paths = {"git": "/usr/bin/git", "gh": "/usr/bin/gh"}
    monkeypatch.setattr("shutil.which", lambda name: fake_paths.get(name))
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")
    ctx = _make_ctx(
        tmp_path,
        env={
            "DANI_AGENT_RUNTIME": "omo",
            "DANI_OPENCODE_SERVER_URL": "http://127.0.0.1:4096",
        },
    )
    result = _check_binaries(ctx)
    assert result.status == CheckStatus.OK
    opencode_rec = next(r for r in result.details["binaries"] if r["name"] == "opencode")
    assert opencode_rec["severity"] == "skip"


def test_binaries_omo_runtime_fails_without_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")
    ctx = _make_ctx(tmp_path, env={"DANI_AGENT_RUNTIME": "omo"})
    result = _check_binaries(ctx)
    assert result.status == CheckStatus.FAIL
    assert "opencode" in result.summary


def test_storage_files_ok(populated_data_dir: Path):
    snapshot, errors = read_only_snapshot(populated_data_dir)
    ctx = _make_ctx(populated_data_dir, snapshot=snapshot, snapshot_errors=errors)
    result = _check_storage_files(ctx)
    assert result.status == CheckStatus.OK


def test_storage_files_fail_on_corrupt_jobs(tmp_path: Path):
    (tmp_path / "jobs.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "registry.json").write_text(json.dumps({"repos": []}), encoding="utf-8")
    (tmp_path / "sessions.json").write_text(json.dumps({"sessions": []}), encoding="utf-8")
    snapshot, errors = read_only_snapshot(tmp_path, retry_delay_s=0.0)
    ctx = _make_ctx(tmp_path, snapshot=snapshot, snapshot_errors=errors)
    result = _check_storage_files(ctx)
    assert result.status == CheckStatus.FAIL
    assert "jobs.json" in result.summary


def test_storage_files_warns_on_events_jsonl_last_line_invalid(tmp_path: Path):
    (tmp_path / "registry.json").write_text(json.dumps({"repos": []}), encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    (tmp_path / "sessions.json").write_text(json.dumps({"sessions": []}), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(json.dumps({"a": 1}) + "\n" + "{nope\n", encoding="utf-8")
    snapshot, errors = read_only_snapshot(tmp_path, retry_delay_s=0.0)
    ctx = _make_ctx(tmp_path, snapshot=snapshot, snapshot_errors=errors)
    result = _check_storage_files(ctx)
    assert result.status == CheckStatus.WARN
    assert "events.jsonl" in result.summary


def test_registered_repos_skip_when_empty(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    result = _check_registered_repos(ctx)
    assert result.status == CheckStatus.SKIP


def test_registered_repos_fail_missing_local_path(tmp_path: Path):
    ctx = _make_ctx(
        tmp_path,
        snapshot={
            "registry": {
                "repos": [
                    {
                        "full_name": "x/y",
                        "local_path": str(tmp_path / "does-not-exist"),
                        "main_branch": "main",
                        "dev_branch": "dev",
                        "enabled": True,
                    }
                ]
            },
            "jobs": {"jobs": []},
            "sessions": {"sessions": []},
            "processed_events": {"keys": []},
            "terminal_targets": {"prs": [], "issues": []},
            "events_jsonl_meta": {"exists": False, "size_bytes": 0},
        },
    )
    result = _check_registered_repos(ctx)
    assert result.status == CheckStatus.FAIL


def test_registered_repos_ok_with_real_git_repo(tmp_path: Path):
    import subprocess

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], check=True, cwd=repo_path)  # noqa: S603, S607

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "x@y")
    _git("config", "user.name", "x")
    (repo_path / "README").write_text("hello", encoding="utf-8")
    _git("add", ".")
    _git("commit", "-q", "-m", "init")
    _git("branch", "dev")
    ctx = _make_ctx(
        tmp_path,
        snapshot={
            "registry": {
                "repos": [
                    {
                        "full_name": "x/y",
                        "local_path": str(repo_path),
                        "main_branch": "main",
                        "dev_branch": "dev",
                        "enabled": True,
                    }
                ]
            },
            "jobs": {"jobs": []},
            "sessions": {"sessions": []},
            "processed_events": {"keys": []},
            "terminal_targets": {"prs": [], "issues": []},
            "events_jsonl_meta": {"exists": False, "size_bytes": 0},
        },
        timeout_seconds=10.0,
    )
    result = _check_registered_repos(ctx)
    assert result.status == CheckStatus.OK
    repo_record = result.details["repos"][0]
    assert repo_record["is_worktree"] is True
    assert repo_record["main_branch_ok"] is True
    assert repo_record["dev_branch_ok"] is True


def test_github_auth_skip_no_token(tmp_path: Path):
    ctx = _make_ctx(tmp_path, env={})
    result = _check_github_auth(ctx)
    assert result.status == CheckStatus.SKIP


def test_github_auth_fail_bad_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from github.GithubException import BadCredentialsException

    class FakeGithub:
        def __init__(self, *args, **kwargs):
            pass

        def get_rate_limit(self):
            raise BadCredentialsException(401, "bad", None)

    monkeypatch.setattr("github.Github", FakeGithub)
    ctx = _make_ctx(tmp_path, env={"DANI_GITHUB_TOKEN": "bad-token-XYZ"})
    result = _check_github_auth(ctx)
    assert result.status == CheckStatus.FAIL
    rendered = json.dumps(result.details) + result.summary
    assert "bad-token-XYZ" not in rendered


def test_github_auth_warn_low_rate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class FakeCore:
        remaining = 5
        limit = 5000
        reset = datetime.now(tz=timezone.utc)

    class FakeRate:
        core = FakeCore()

    class FakeGithub:
        def __init__(self, *args, **kwargs):
            pass

        def get_rate_limit(self):
            return FakeRate()

    monkeypatch.setattr("github.Github", FakeGithub)
    ctx = _make_ctx(tmp_path, env={"DANI_GITHUB_TOKEN": "good"})
    result = _check_github_auth(ctx)
    assert result.status == CheckStatus.WARN
    assert "5/5000" in result.summary


def test_github_auth_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class FakeCore:
        remaining = 4000
        limit = 5000
        reset = datetime.now(tz=timezone.utc)

    class FakeRate:
        core = FakeCore()

    class FakeGithub:
        def __init__(self, *args, **kwargs):
            pass

        def get_rate_limit(self):
            return FakeRate()

    monkeypatch.setattr("github.Github", FakeGithub)
    ctx = _make_ctx(tmp_path, env={"DANI_GITHUB_TOKEN": "good"})
    result = _check_github_auth(ctx)
    assert result.status == CheckStatus.OK


def test_server_health_skip_when_not_listening(tmp_path: Path):
    ctx = _make_ctx(tmp_path, port=1)
    result = _check_server_health(ctx)
    assert result.status == CheckStatus.SKIP
    assert result.details["listening"] is False


def test_server_health_ok_with_local_server(tmp_path: Path):
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ctx = _make_ctx(tmp_path, port=port)
        result = _check_server_health(ctx)
        assert result.status == CheckStatus.OK
        assert result.details["http_status"] == 200
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def test_server_health_warn_on_wrong_body(tmp_path: Path):
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello world")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ctx = _make_ctx(tmp_path, port=port)
        result = _check_server_health(ctx)
        assert result.status == CheckStatus.WARN
        assert result.details["likely_not_dani"] is True
    finally:
        server.shutdown()
        thread.join(timeout=2.0)


def _job(
    job_id: str,
    *,
    status: str,
    updated_at: datetime,
    stage: str = "implementation",
    repo: str = "x/y",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "status": status,
        "stage": stage,
        "repo_full_name": repo,
        "updated_at": updated_at.isoformat(),
        "created_at": updated_at.isoformat(),
    }


def _session(
    session_id: str,
    *,
    status: str,
    updated_at: datetime,
    job_id: str | None = None,
    stage: str = "implementation",
    repo: str = "x/y",
) -> dict[str, Any]:
    return {
        "id": session_id,
        "status": status,
        "stage": stage,
        "repo_full_name": repo,
        "updated_at": updated_at.isoformat(),
        "created_at": updated_at.isoformat(),
        "job_id": job_id,
    }


def test_stuck_jobs_ok(tmp_path: Path):
    now = datetime.now(tz=timezone.utc)
    ctx = _make_ctx(
        tmp_path,
        snapshot={
            "jobs": {"jobs": [_job("j1", status="completed", updated_at=now)]},
            "sessions": {"sessions": []},
        },
        now_utc=now,
    )
    result = _check_stuck_jobs(ctx)
    assert result.status == CheckStatus.OK


def test_stuck_jobs_warn(tmp_path: Path):
    now = datetime.now(tz=timezone.utc)
    stale = now - timedelta(hours=10)
    ctx = _make_ctx(
        tmp_path,
        snapshot={
            "jobs": {"jobs": [_job("j1", status="launched", updated_at=stale)]},
            "sessions": {"sessions": []},
        },
        now_utc=now,
        thresholds={"stuck_job_age_seconds": 3600},
    )
    result = _check_stuck_jobs(ctx)
    assert result.status == CheckStatus.WARN
    assert result.details["stuck_count"] == 1


def test_stuck_sessions_fail_on_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(hours=10)
    snapshot = {
        "jobs": {"jobs": [_job("j1", status="completed", updated_at=old)]},
        "sessions": {"sessions": [_session("s1", status="launched", updated_at=old, job_id="j1")]},
    }
    monkeypatch.setattr("dani.doctor.read_only_snapshot", lambda *a, **kw: (snapshot, {}))
    ctx = _make_ctx(tmp_path, snapshot=snapshot, now_utc=now)
    result = _check_stuck_sessions(ctx)
    assert result.status == CheckStatus.FAIL
    assert len(result.details["drift"]) == 1


def test_stuck_sessions_drift_in_grace_window_not_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(tz=timezone.utc)
    fresh = now - timedelta(seconds=10)
    snapshot = {
        "jobs": {"jobs": [_job("j1", status="completed", updated_at=fresh)]},
        "sessions": {"sessions": [_session("s1", status="launched", updated_at=fresh, job_id="j1")]},
    }
    monkeypatch.setattr("dani.doctor.read_only_snapshot", lambda *a, **kw: (snapshot, {}))
    ctx = _make_ctx(tmp_path, snapshot=snapshot, now_utc=now)
    result = _check_stuck_sessions(ctx)
    assert result.status == CheckStatus.OK


def test_stuck_sessions_orphan_session_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(hours=10)
    snapshot = {
        "jobs": {"jobs": []},
        "sessions": {"sessions": [_session("s1", status="launched", updated_at=old, job_id="missing")]},
    }
    monkeypatch.setattr("dani.doctor.read_only_snapshot", lambda *a, **kw: (snapshot, {}))
    ctx = _make_ctx(tmp_path, snapshot=snapshot, now_utc=now)
    result = _check_stuck_sessions(ctx)
    assert result.status == CheckStatus.WARN
    assert len(result.details["orphans"]) == 1


def test_stuck_sessions_long_running_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(tz=timezone.utc)
    very_old = now - timedelta(days=10)
    snapshot = {
        "jobs": {"jobs": [_job("j1", status="launched", updated_at=very_old)]},
        "sessions": {"sessions": [_session("s1", status="launched", updated_at=very_old, job_id="j1")]},
    }
    monkeypatch.setattr("dani.doctor.read_only_snapshot", lambda *a, **kw: (snapshot, {}))
    ctx = _make_ctx(tmp_path, snapshot=snapshot, now_utc=now)
    result = _check_stuck_sessions(ctx)
    assert result.status == CheckStatus.WARN
    assert len(result.details["long_running"]) == 1


def test_disk_usage_ok(populated_data_dir: Path):
    ctx = _make_ctx(populated_data_dir)
    result = _check_disk_usage(ctx)
    assert result.status == CheckStatus.OK


def test_disk_usage_fail_when_threshold_exceeded(populated_data_dir: Path):
    big = populated_data_dir / "jobs.json"
    big.write_text("x" * 1024, encoding="utf-8")
    ctx = _make_ctx(populated_data_dir, thresholds={"jobs_bytes_fail": 100, "jobs_bytes_warn": 50})
    result = _check_disk_usage(ctx)
    assert result.status == CheckStatus.FAIL


def test_disk_usage_warn_on_runs(populated_data_dir: Path):
    runs = populated_data_dir / "runs"
    runs.mkdir()
    (runs / "blob").write_text("hi" * 200, encoding="utf-8")
    ctx = _make_ctx(populated_data_dir, thresholds={"runs_bytes_warn": 1, "runs_bytes_fail": 1_000_000_000})
    result = _check_disk_usage(ctx)
    assert result.status == CheckStatus.WARN


def test_backup_files_ok(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    result = _check_backup_files(ctx)
    assert result.status == CheckStatus.OK


def test_backup_files_warn_on_count(tmp_path: Path):
    for i in range(5):
        (tmp_path / f"jobs.json.bak.{i}").write_text("x", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    result = _check_backup_files(ctx)
    assert result.status == CheckStatus.WARN
    assert "count 5" in result.summary


def test_process_sprawl_skip_when_ps_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("dani.doctor._ps_run", lambda *a, **kw: (-1, []))
    ctx = _make_ctx(tmp_path)
    result = _check_process_sprawl(ctx)
    assert result.status == CheckStatus.SKIP


def test_process_sprawl_ok_under_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "dani.doctor._ps_run",
        lambda *a, **kw: (0, ["1234 1 01:23"]),
    )
    monkeypatch.setattr("dani.doctor._classify_ps_command", lambda cmd: "omx_exec" if "omx" in cmd else None)
    monkeypatch.setattr(
        "dani.doctor._ps_run",
        lambda args, **kw: (0, ["1234 1 01:23"]) if "-axo" in args else (0, ["omx exec something"]),
    )
    ctx = _make_ctx(tmp_path)
    result = _check_process_sprawl(ctx)
    assert result.status == CheckStatus.OK


def test_process_sprawl_warn_over_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pids = [f"{i} 1 01:23" for i in range(25)]

    def fake_ps_run(args, *, timeout_s):
        if "-axo" in args:
            return 0, pids
        return 0, ["omx exec full prompt that should never leak"]

    monkeypatch.setattr("dani.doctor._ps_run", fake_ps_run)
    ctx = _make_ctx(tmp_path)
    result = _check_process_sprawl(ctx)
    assert result.status == CheckStatus.WARN
    rendered = json.dumps(result.details)
    assert "full prompt that should never leak" not in rendered
    for sample in result.details["sample"]:
        assert "command" not in sample
        assert "argv" not in sample


# ============================================================
# W7: end-to-end + exit code matrix
# ============================================================


def test_e2e_happy_path_runs_all_checks(populated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    fake_paths = {
        "git": "/usr/bin/git",
        "gh": "/usr/bin/gh",
        "omx": "/usr/bin/omx",
        "codex": "/usr/bin/codex",
    }
    monkeypatch.setattr("shutil.which", lambda name: fake_paths.get(name))
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")

    class FakeCore:
        remaining = 4000
        limit = 5000
        reset = datetime.now(tz=timezone.utc)

    class FakeRate:
        core = FakeCore()

    class FakeGithub:
        def __init__(self, *args, **kwargs):
            pass

        def get_rate_limit(self):
            return FakeRate()

    monkeypatch.setattr("github.Github", FakeGithub)
    monkeypatch.setattr("dani.doctor._ps_run", lambda *a, **kw: (0, []))

    runner = CliRunner()
    env = {
        "DANI_WEBHOOK_SECRET": "secret",
        "DANI_GITHUB_TOKEN": "tok",
        "DANI_AGENT_RUNTIME": "omx",
    }
    result = runner.invoke(app, ["doctor", "--data-dir", str(populated_data_dir), "--json"], env=env)
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["overall_status"] in {"ok", "warn"}
    assert payload["summary"]["fail"] == 0
    names = {r["name"] for r in payload["results"]}
    expected = {
        "config_env",
        "binaries",
        "storage_files",
        "registered_repos",
        "github_auth",
        "server_health",
        "stuck_jobs",
        "stuck_sessions",
        "disk_usage",
        "backup_files",
        "process_sprawl",
    }
    assert expected.issubset(names)


def test_e2e_failure_mode_drift_exits_2(populated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(tz=timezone.utc)
    old = (now - timedelta(hours=10)).isoformat()
    (populated_data_dir / "jobs.json").write_text(
        json.dumps({
            "jobs": [
                {
                    "id": "j1",
                    "status": "completed",
                    "stage": "implementation",
                    "repo_full_name": "x/y",
                    "updated_at": old,
                    "created_at": old,
                }
            ]
        }),
        encoding="utf-8",
    )
    (populated_data_dir / "sessions.json").write_text(
        json.dumps({
            "sessions": [
                {
                    "id": "s1",
                    "status": "launched",
                    "stage": "implementation",
                    "repo_full_name": "x/y",
                    "job_id": "j1",
                    "updated_at": old,
                    "created_at": old,
                }
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")
    monkeypatch.setattr("dani.doctor._ps_run", lambda *a, **kw: (0, []))

    class FakeGithub:
        def __init__(self, *args, **kwargs):
            pass

        def get_rate_limit(self):
            class C:
                remaining = 4000
                limit = 5000
                reset = datetime.now(tz=timezone.utc)

            class R:
                core = C()

            return R()

    monkeypatch.setattr("github.Github", FakeGithub)

    runner = CliRunner()
    env = {"DANI_WEBHOOK_SECRET": "x", "DANI_GITHUB_TOKEN": "y"}
    result = runner.invoke(
        app,
        ["doctor", "--data-dir", str(populated_data_dir), "--check", "stuck_sessions", "--json"],
        env=env,
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["overall_status"] == "fail"
    stuck = payload["results"][0]
    assert stuck["name"] == "stuck_sessions"
    assert stuck["status"] == "fail"
    assert len(stuck["details"]["drift"]) == 1


def test_e2e_strict_warn_exits_1(populated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    for i in range(5):
        (populated_data_dir / f"jobs.json.bak.{i}").write_text("x", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")
    monkeypatch.setattr("dani.doctor._ps_run", lambda *a, **kw: (0, []))

    class FakeGithub:
        def __init__(self, *args, **kwargs):
            pass

        def get_rate_limit(self):
            class C:
                remaining = 4000
                limit = 5000
                reset = datetime.now(tz=timezone.utc)

            class R:
                core = C()

            return R()

    monkeypatch.setattr("github.Github", FakeGithub)
    runner = CliRunner()
    env = {"DANI_WEBHOOK_SECRET": "x", "DANI_GITHUB_TOKEN": "y"}
    result = runner.invoke(
        app,
        ["doctor", "--data-dir", str(populated_data_dir), "--check", "backup_files", "--strict"],
        env=env,
    )
    assert result.exit_code == 1


def test_e2e_text_output_renders(populated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")
    monkeypatch.setattr("dani.doctor._ps_run", lambda *a, **kw: (0, []))
    monkeypatch.setenv("NO_COLOR", "1")

    runner = CliRunner()
    env = {"DANI_WEBHOOK_SECRET": "x", "DANI_GITHUB_TOKEN": "y", "NO_COLOR": "1"}
    result = runner.invoke(
        app,
        ["doctor", "--data-dir", str(populated_data_dir), "--check", "config_env"],
        env=env,
    )
    assert result.exit_code == 0
    assert "config_env" in result.output
    assert "\x1b" not in result.output


def test_e2e_exit_code_matrix_parametrized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")
    monkeypatch.setattr("dani.doctor._ps_run", lambda *a, **kw: (0, []))

    runner = CliRunner()
    cases = [
        ([], 0),
        (["--strict"], 0),
    ]
    for cli_args, expected_exit in cases:
        result = runner.invoke(
            app,
            ["doctor", "--data-dir", str(tmp_path), "--check", "process_sprawl", *cli_args],
            env={},
        )
        assert result.exit_code == expected_exit, (cli_args, result.output)


def test_e2e_no_token_in_output(populated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    ULTRA_SECRET = "ghp_THIS_MUST_NEVER_APPEAR_IN_OUTPUT_99999"
    WEBHOOK_SECRET = "this-webhook-secret-must-never-appear-12345"
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("dani.doctor._probe_binary_version", lambda binary, *, timeout_seconds: "v1")
    monkeypatch.setattr("dani.doctor._ps_run", lambda *a, **kw: (0, []))

    class FakeGithub:
        def __init__(self, *args, **kwargs):
            pass

        def get_rate_limit(self):
            class C:
                remaining = 4000
                limit = 5000
                reset = datetime.now(tz=timezone.utc)

            class R:
                core = C()

            return R()

    monkeypatch.setattr("github.Github", FakeGithub)
    runner = CliRunner()
    env = {"DANI_WEBHOOK_SECRET": WEBHOOK_SECRET, "DANI_GITHUB_TOKEN": ULTRA_SECRET}
    result = runner.invoke(
        app,
        ["doctor", "--data-dir", str(populated_data_dir), "--json", "--verbose"],
        env=env,
    )
    assert result.exit_code in (0, 1, 2)
    assert ULTRA_SECRET not in result.output
    assert WEBHOOK_SECRET not in result.output
