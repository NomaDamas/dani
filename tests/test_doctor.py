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
    CHECK_REGISTRY.clear()
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
    report = run_doctor(tmp_path)
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
    assert "doctor" in result.output.lower()
    assert "--data-dir" in result.output
    assert "--json" in result.output
    assert "--strict" in result.output


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
    result = runner.invoke(app, ["doctor", "--data-dir", str(tmp_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["overall_status"] in {"ok", "skip"}


def test_allowed_threshold_keys_is_frozenset():
    assert isinstance(ALLOWED_THRESHOLD_KEYS, frozenset)
    assert "runs_bytes_warn" in ALLOWED_THRESHOLD_KEYS
    assert "unknown_random_key" not in ALLOWED_THRESHOLD_KEYS
