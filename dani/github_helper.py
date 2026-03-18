from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

if __package__ in {None, ""}:
    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parent
    if str(package_dir) in sys.path:
        sys.path.remove(str(package_dir))
    sys.path.insert(0, str(repo_root))

from dani.github import GitHubCLI

app = typer.Typer(help="PyGithub-backed helper commands for dani agents.")
REPO_OPTION = typer.Option(..., "--repo", help="owner/name")
ISSUE_OPTION = typer.Option(..., "--issue", help="Issue number")
PR_OPTION = typer.Option(..., "--pr", help="Pull request number")
HEAD_OPTION = typer.Option(..., "--head", help="Head branch")
BASE_OPTION = typer.Option(..., "--base", help="Base branch")
TITLE_OPTION = typer.Option(..., "--title", help="Pull request title")
BODY_OPTION = typer.Option(None, "--body", help="Comment or pull request body")
BODY_FILE_OPTION = typer.Option(None, "--body-file", help="Path to a UTF-8 markdown/body file")


def _read_body(body: str | None, body_file: Path | None) -> str:
    if body is not None and body_file is not None:
        msg = "Use either --body or --body-file, not both"
        raise typer.BadParameter(msg)
    if body_file is not None:
        return body_file.read_text(encoding="utf-8")
    if body is not None:
        return body
    msg = "Provide --body or --body-file"
    raise typer.BadParameter(msg)


def _echo(payload: dict[str, object]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("issue-comment")
def issue_comment(
    repo: str = REPO_OPTION,
    issue: int = ISSUE_OPTION,
    body: str | None = BODY_OPTION,
    body_file: Path | None = BODY_FILE_OPTION,
) -> None:
    payload = GitHubCLI().create_issue_comment(repo, issue, _read_body(body, body_file))
    _echo(payload)


@app.command("pr-comment")
def pr_comment(
    repo: str = REPO_OPTION,
    pr: int = PR_OPTION,
    body: str | None = BODY_OPTION,
    body_file: Path | None = BODY_FILE_OPTION,
) -> None:
    payload = GitHubCLI().create_pr_comment(repo, pr, _read_body(body, body_file))
    _echo(payload)


@app.command("ensure-pr")
def ensure_pr(
    repo: str = REPO_OPTION,
    head: str = HEAD_OPTION,
    base: str = BASE_OPTION,
    title: str = TITLE_OPTION,
    body: str | None = BODY_OPTION,
    body_file: Path | None = BODY_FILE_OPTION,
) -> None:
    payload = GitHubCLI().ensure_pull_request(
        repo,
        head=head,
        base=base,
        title=title,
        body=_read_body(body, body_file),
    )
    _echo(payload)


if __name__ == "__main__":
    app()
