from __future__ import annotations

import json
import os
from pathlib import Path

import typer
import uvicorn

from dani.models import DaniConfig
from dani.server import create_app
from dani.service import DaniService

app = typer.Typer(help="Simple GitHub webhook -> OMX automation loop.")
DEFAULT_DATA_DIR = Path(".dani")
DATA_DIR_OPTION = typer.Option(DEFAULT_DATA_DIR, help="Directory for dani state files.")
HOST_OPTION = typer.Option("127.0.0.1", help="Bind host.")
PORT_OPTION = typer.Option(8787, help="Bind port.")
FULL_NAME_ARGUMENT = typer.Argument(..., help="owner/name")
LOCAL_PATH_ARGUMENT = typer.Argument(..., help="Local checkout path")
MAIN_BRANCH_OPTION = typer.Option("main", help="Main branch name.")
DEV_BRANCH_OPTION = typer.Option("dev", help="Development branch name.")


def build_config(data_dir: Path, host: str = "127.0.0.1", port: int = 8787) -> DaniConfig:
    secret = os.environ.get("DANI_WEBHOOK_SECRET", "")
    return DaniConfig(data_dir=data_dir, webhook_secret=secret, host=host, port=port)


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
    """Queue open issues for a registered repository."""
    service = build_service(data_dir=data_dir)
    count = service.bootstrap_repo(repo_full_name)
    typer.echo(f"queued {count} issues")


@app.command("show-state")
def show_state(data_dir: Path = DATA_DIR_OPTION) -> None:
    """Print current dani state."""
    service = build_service(data_dir=data_dir)
    typer.echo(json.dumps(service.state_snapshot(), ensure_ascii=False, indent=2))
