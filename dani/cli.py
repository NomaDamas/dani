from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

import typer
import uvicorn

from dani import doctor as doctor_module
from dani.doctor import (
    CheckStatus,
    _OutputPathError,
    _ThresholdParseError,
    format_json,
    format_text,
    parse_thresholds,
    resolve_port,
    run_doctor,
    validate_output_path,
)
from dani.models import DEFAULT_AGENT_TIMEOUT_SECONDS, DEFAULT_MAX_ISSUE_FOLLOWUPS, DaniConfig
from dani.server import create_app
from dani.service import DaniService

app = typer.Typer(help="Simple GitHub webhook -> OMX automation loop.")
DEFAULT_DATA_DIR = Path.home() / ".dani"
DATA_DIR_OPTION = typer.Option(DEFAULT_DATA_DIR, help="Directory for dani state files.")
HOST_OPTION = typer.Option("127.0.0.1", help="Bind host.")
PORT_OPTION = typer.Option(8787, help="Bind port.")
FULL_NAME_ARGUMENT = typer.Argument(..., help="owner/name")
LOCAL_PATH_ARGUMENT = typer.Argument(..., help="Local checkout path")
MAIN_BRANCH_OPTION = typer.Option("main", help="Main branch name.")
DEV_BRANCH_OPTION = typer.Option("dev", help="Development branch name.")


def _load_config_file(data_dir: Path) -> dict[str, object]:
    config_path = data_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"Invalid dani config file {config_path}: {exc}"
        raise typer.BadParameter(msg) from exc
    if not isinstance(payload, dict):
        msg = f"Invalid dani config file {config_path}: top-level JSON value must be an object"
        raise typer.BadParameter(msg)
    return payload


def _parse_positive_float(value: object, *, name: str) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        msg = f"{name} must be a number of seconds"
        raise typer.BadParameter(msg) from exc
    if parsed <= 0:
        msg = f"{name} must be greater than 0"
        raise typer.BadParameter(msg)
    return parsed


def _resolve_agent_timeout_seconds(config_payload: dict[str, object]) -> float:
    value = config_payload.get("agent_timeout_seconds", DEFAULT_AGENT_TIMEOUT_SECONDS)
    env_value = os.environ.get("DANI_AGENT_TIMEOUT_SECONDS")
    if env_value:
        value = env_value
    return _parse_positive_float(value, name="agent_timeout_seconds")


def _resolve_bot_login(config_payload: dict[str, object]) -> str | None:
    env_value = os.environ.get("DANI_BOT_LOGIN")
    if env_value is not None and env_value != "":
        return env_value
    raw = config_payload.get("bot_login")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _parse_non_negative_int(value: object, *, name: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        msg = f"{name} must be an integer"
        raise typer.BadParameter(msg) from exc
    if parsed < 0:
        msg = f"{name} must be greater than or equal to 0"
        raise typer.BadParameter(msg)
    return parsed


def _resolve_max_issue_followups(config_payload: dict[str, object]) -> int:
    value: object = config_payload.get("max_issue_followups", DEFAULT_MAX_ISSUE_FOLLOWUPS)
    env_value = os.environ.get("DANI_MAX_ISSUE_FOLLOWUPS")
    if env_value:
        value = env_value
    return _parse_non_negative_int(value, name="max_issue_followups")


def build_config(data_dir: Path, host: str = "127.0.0.1", port: int = 8787) -> DaniConfig:
    config_payload = _load_config_file(data_dir)
    secret = os.environ.get("DANI_WEBHOOK_SECRET", "")
    agent_runtime = os.environ.get("DANI_AGENT_RUNTIME") or str(config_payload.get("agent_runtime", "omx"))
    agent_timeout_seconds = _resolve_agent_timeout_seconds(config_payload)
    bot_login = _resolve_bot_login(config_payload)
    max_issue_followups = _resolve_max_issue_followups(config_payload)
    return DaniConfig(
        data_dir=data_dir,
        webhook_secret=secret,
        host=host,
        port=port,
        agent_runtime=agent_runtime,
        agent_timeout_seconds=agent_timeout_seconds,
        bot_login=bot_login,
        max_issue_followups=max_issue_followups,
    )


def build_service(data_dir: Path, host: str = "127.0.0.1", port: int = 8787) -> DaniService:
    return DaniService(build_config(data_dir=data_dir, host=host, port=port))


@app.command()
def serve(
    data_dir: Path = DATA_DIR_OPTION,
    host: str = HOST_OPTION,
    port: int = PORT_OPTION,
) -> None:
    """Start the GitHub webhook server."""
    if not os.environ.get("DANI_WEBHOOK_SECRET"):
        msg = "DANI_WEBHOOK_SECRET must be set"
        raise typer.BadParameter(msg)
    service = build_service(data_dir=data_dir, host=host, port=port)
    uvicorn.run(create_app(service), host=host, port=port)


@app.command("register-repo")
def register_repo(
    full_name: str = FULL_NAME_ARGUMENT,
    local_path: Path = LOCAL_PATH_ARGUMENT,
    data_dir: Path = DATA_DIR_OPTION,
    main_branch: str = MAIN_BRANCH_OPTION,
    dev_branch: str = DEV_BRANCH_OPTION,
) -> None:
    """Register a repository for webhook processing."""
    service = build_service(data_dir=data_dir)
    repo = service.register_repo(
        full_name=full_name, local_path=str(local_path), main_branch=main_branch, dev_branch=dev_branch
    )
    typer.echo(json.dumps(repo.to_dict(), ensure_ascii=False, indent=2))


@app.command()
def bootstrap(
    repo_full_name: str = FULL_NAME_ARGUMENT,
    data_dir: Path = DATA_DIR_OPTION,
) -> None:
    """Bootstrap open issues for a registered repository and wait for completion."""
    service = build_service(data_dir=data_dir)
    count = service.bootstrap_repo(repo_full_name)
    service.wait_for_idle()
    typer.echo(f"processed {count} issues")


@app.command("show-state")
def show_state(data_dir: Path = DATA_DIR_OPTION) -> None:
    """Print current dani state."""
    service = build_service(data_dir=data_dir)
    typer.echo(json.dumps(service.state_snapshot(), ensure_ascii=False, indent=2))


@app.command("restart-issue")
def restart_issue(
    repo_full_name: str = FULL_NAME_ARGUMENT,
    issue_number: int = typer.Argument(..., help="Issue number"),
    data_dir: Path = DATA_DIR_OPTION,
) -> None:
    """Supersede stale issue jobs, run a fresh issue_request, and wait for completion."""
    service = build_service(data_dir=data_dir)
    job = service.restart_issue(repo_full_name, issue_number)
    service.wait_for_idle()
    typer.echo(json.dumps(job.to_dict(), ensure_ascii=False, indent=2))


DOCTOR_JSON_OPTION = typer.Option(False, "--json", "-j", help="Emit JSON instead of text.")
DOCTOR_CHECK_OPTION = typer.Option(None, "--check", help="Restrict to the named check. Repeatable.")
DOCTOR_VERBOSE_OPTION = typer.Option(False, "--verbose", "-v", help="Include per-check details.")
DOCTOR_STRICT_OPTION = typer.Option(False, "--strict", help="Treat warnings as exit 1.")
DOCTOR_TIMEOUT_OPTION = typer.Option(5.0, "--timeout", help="Per-check soft deadline in seconds.")
DOCTOR_PORT_OPTION = typer.Option(None, "--port", help="Port for server_health probe (default: DANI_PORT env or 8787).")
DOCTOR_OUTPUT_OPTION = typer.Option(
    None, "--output", help="Write report to PATH instead of stdout. Must NOT be under data_dir."
)
DOCTOR_THRESHOLD_OPTION = typer.Option(
    None,
    "--threshold",
    help="Override threshold as KEY=VALUE (repeatable; strict allow-list).",
)


def _doctor_exit_code(overall: CheckStatus, strict: bool) -> int:
    if overall == CheckStatus.FAIL:
        return 2
    if overall == CheckStatus.WARN and strict:
        return 1
    return 0


def _doctor_render(report, *, json_output: bool, verbose: bool, no_color: bool, output_path: Path | None) -> str:
    if json_output:
        return format_json(report)
    use_color = (not no_color) and sys.stdout.isatty() and output_path is None
    return format_text(report, verbose=verbose, use_color=use_color)


def _doctor_parse_options(
    *,
    threshold: list[str] | None,
    output: Path | None,
    data_dir: Path,
    port: int | None,
) -> tuple[dict[str, int], Path | None, int]:
    try:
        thresholds = parse_thresholds(list(threshold or []))
    except _ThresholdParseError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        resolved_output = validate_output_path(output, data_dir)
    except _OutputPathError as exc:
        raise typer.BadParameter(str(exc)) from exc
    resolved_port = resolve_port(port, os.environ)
    return thresholds, resolved_output, resolved_port


@app.command("doctor")
def doctor(
    data_dir: Path = DATA_DIR_OPTION,
    json_output: bool = DOCTOR_JSON_OPTION,
    check: list[str] | None = DOCTOR_CHECK_OPTION,
    verbose: bool = DOCTOR_VERBOSE_OPTION,
    strict: bool = DOCTOR_STRICT_OPTION,
    timeout: float = DOCTOR_TIMEOUT_OPTION,
    port: int | None = DOCTOR_PORT_OPTION,
    output: Path | None = DOCTOR_OUTPUT_OPTION,
    threshold: list[str] | None = DOCTOR_THRESHOLD_OPTION,
) -> None:
    """Run read-only health diagnostics on a dani installation."""
    _ = doctor_module
    try:
        thresholds, resolved_output, resolved_port = _doctor_parse_options(
            threshold=threshold, output=output, data_dir=data_dir, port=port
        )
        no_color = bool(os.environ.get("NO_COLOR"))
        report = run_doctor(
            data_dir=data_dir,
            check_names=list(check) if check else None,
            verbose=verbose,
            timeout=timeout,
            thresholds=thresholds,
            port=resolved_port,
            env=os.environ,
            no_color=no_color,
        )
        rendered = _doctor_render(
            report,
            json_output=json_output,
            verbose=verbose,
            no_color=no_color,
            output_path=resolved_output,
        )
        if resolved_output is not None:
            resolved_output.write_text(rendered + "\n", encoding="utf-8")
        else:
            typer.echo(rendered)
        raise typer.Exit(_doctor_exit_code(report.overall_status, strict))  # noqa: TRY301
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as exc:
        msg = str(exc)
        if json_output:
            typer.echo(
                json.dumps({"schema_version": 1, "error": msg[:400]}),
                err=True,
            )
        else:
            typer.echo(f"dani doctor crashed: {msg[:400]}", err=True)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        raise typer.Exit(3) from exc
