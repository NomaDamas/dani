# dani

> [!WARNING]
> Experimental project: dani is suitable for maintainer-operated trials and local automation experiments, not unattended production use. Expect breaking changes in `0.x` releases.

Simple GitHub webhook -> agent automation loop. Supports **auto** stage routing
across Gajae-Code (`gjc`) and Codex.

## Quickstart

Install the CLI:

```bash
uv tool install dani
dani --help
```

For source checkouts, use `uv run dani ...` from this repository instead.

Prepare a local repository checkout and credentials:

```bash
export DANI_WEBHOOK_SECRET="$(openssl rand -hex 32)"
export DANI_GITHUB_TOKEN="github-token-with-repo-access"
export DANI_AGENT_RUNTIME=auto

dani register-repo owner/name /absolute/path/to/repo
dani doctor
dani serve --data-dir ~/.dani
```

Create a GitHub webhook for the repository or organization:

- Payload URL: the public URL that forwards to `http://127.0.0.1:8787/webhook`
- Content type: `application/json`
- Secret: the same value as `DANI_WEBHOOK_SECRET`
- Events: issues, issue comments, pull requests, pull request reviews, and pull request review comments

Before trusting it with important repositories, run `dani doctor --json` and a test issue in a disposable repository.

## What v1 includes
- Typer CLI
- FastAPI webhook server
- Registered repos only
- Repo-serial / cross-repo parallel job handling
- Stage-routed agent runtime: Gajae-Code headless planning/final verdict and Codex implementation/review
- Separate prompt templates in `dani/prompts.py`
- Workflows for:
  - issue request report
  - `/approve` implementation
  - 3 review rounds for agent-authored PRs
  - external contributor account-age eligibility checks
  - event-driven, duplicate-delivery-safe re-review for external contributor PRs
  - final verdict + auto-merge on APPROVE

## Environment
Required local tools:
- `git`
- `codex` — required for implementation and review stages
- `gjc` — required for issue planning and final verdict stages

Required environment variables:
- `DANI_WEBHOOK_SECRET`
- `DANI_GITHUB_TOKEN` (preferred) or `GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_PAT`

Optional environment variables:
- `DANI_AGENT_RUNTIME` — selects the agent backend. Accepted values:
  - `auto` (default): Gajae-Code for issue planning/final verdict, Codex for implementation/review
  - `codex`
  - `gajae` / `gajae-code` / `gjc`
- `DANI_AGENT_TIMEOUT_SECONDS` — overrides the per-job agent wait timeout in seconds.
- `DANI_GAJAE_BIN` — Gajae-Code executable name, default `gjc`.
- `DANI_GAJAE_MODEL` — default Gajae-Code model, default `nomadamas/gpt-5.5`.
- `DANI_GAJAE_PLAN_MODEL` — issue-planning model, default `nomadamas-anthropic/opus-4.8`.
- `DANI_GAJAE_FINAL_MODEL` — final-verdict model, default `nomadamas/gpt-5.5`.

Optional config file (`~/.dani/config.json` by default, or `<data-dir>/config.json`):

```json
{
  "agent_runtime": "auto",
  "agent_timeout_seconds": 3600
}
```

`agent_timeout_seconds` defaults to `3600`. The environment variable
`DANI_AGENT_TIMEOUT_SECONDS` takes precedence over the config file when set.

Implementation prompts instruct Codex to use `ulw-loop tdd manual qa commit
well` for evidence-led delivery. Review prompts use plain Codex code review
guidance. Gajae-Code runs headlessly with `gjc --print` for planning and final
verdict jobs.

## Codex trust prerequisite
Before dani can reliably launch or resume Codex sessions for a repository, that repository directory should be trusted by Codex at least once. In practice, run `codex exec 'hello'` once from the target repo and accept the trust prompt before using dani automation there. Otherwise a trust prompt can block session startup or resume.

## Gajae-Code prerequisite
Install Gajae-Code and verify `gjc --version` works before using `auto` or
`gajae` runtime routing. Gajae-Code model selection is controlled through the
`DANI_GAJAE_*` environment variables above.

## CLI
```bash
dani register-repo owner/name /absolute/path/to/repo
dani serve --data-dir .dani
dani bootstrap owner/name
dani show-state
dani doctor
```

## dani doctor

`dani doctor` runs read-only health diagnostics against a dani installation.
It is safe to run while `dani serve` is live on the same machine: doctor never
writes to `~/.dani/`, never binds the webhook port, never spawns runners or
competes with the serve process for storage locks, and never echoes any value
of `DANI_WEBHOOK_SECRET`/`DANI_GITHUB_TOKEN`/`GITHUB_TOKEN`/`GH_TOKEN`/
`GITHUB_PAT` or any raw `ps` argv.

```bash
# Run every check; human-readable output.
dani doctor

# Machine-readable JSON for monitoring / CI.
dani doctor --json | jq

# Run a subset of checks.
dani doctor --check stuck_sessions --check disk_usage --json

# Treat warnings as exit 1 (e.g., in CI gates).
dani doctor --strict

# Override default thresholds (strict allow-list of keys).
dani doctor --threshold runs_bytes_warn=10000000000 --threshold stuck_job_age_seconds=7200

# Probe a non-default webhook port (default: DANI_PORT env or 8787).
dani doctor --check server_health --port 8000

# Write the report to a file outside ~/.dani/.
dani doctor --json --output /tmp/dani-report.json
```

### Checks

| Name | Description |
|---|---|
| `config_env` | webhook secret, GitHub token, agent runtime, config.json parse status |
| `binaries` | `git`, `gh`, plus runtime-specific `codex` and/or `gjc` |
| `storage_files` | parse-ability of `registry.json`/`jobs.json`/`sessions.json`/`processed-events.json`/`terminal-targets.json`; tolerates one transient parse error per file plus an `events.jsonl` last-line append race |
| `registered_repos` | each registered repo's `local_path` is a git working tree and resolves both `main_branch` and `dev_branch` |
| `github_auth` | resolves the GitHub token, verifies it with the minimum `Github.get_rate_limit()` call, reports rate-limit headroom; never echoes the token, only its source env-var name |
| `server_health` | if `127.0.0.1:<port>` is listening, GET `/health` and compare to `{"status":"ok"}`; SKIP (not FAIL) when no local server |
| `stuck_jobs` | warns when active jobs (`queued`/`launched`/`retrying`/`recovering`) are older than `stuck_job_age_seconds` (default = `agent_timeout_seconds`) |
| `stuck_sessions` | FAILs on confirmed A-class drift (session `launched` while linked job is terminal — re-validated with a 100ms re-snapshot and a 60s grace window); WARNs on long-running launches and orphans |
| `disk_usage` | size of each storage file plus a soft-deadlined walk of `runs/`; thresholds for warn/fail per target |
| `backup_files` | accumulated `*.bak.*` files in `data_dir`: count / oldest age / total bytes |
| `process_sprawl` | counts alive `codex exec` / `gjc --print` processes; reports only PID/PPID/etime/classifier — never raw argv |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | overall `ok` (warnings allowed without `--strict`; also returned when every check skipped) |
| `1` | overall `warn` AND `--strict` |
| `2` | overall `fail` (any check failed, or invalid CLI option) |
| `3` | `dani doctor` itself crashed (unhandled exception); sanitized error to stderr, redacted traceback only with `--verbose` |

### JSON schema

`dani doctor --json` emits a stable schema:

```json
{
  "schema_version": 1,
  "started_at": "...", "finished_at": "...",
  "data_dir": "...",
  "host": {"port": 8787, "platform": "darwin", "python_version": "3.13.0", "dani_version": "0.0.1"},
  "overall_status": "ok|warn|fail|skip",
  "summary": {"ok": N, "warn": N, "fail": N, "skip": N},
  "results": [{"name": "...", "status": "...", "summary": "...", "details": {...}, "duration_ms": N, "error": null}]
}
```

If the schema needs to change, `schema_version` is bumped.

## Persistence
State is stored under `~/.dani/` by default:
- `registry.json`
- `jobs.json`
- `sessions.json`
- `events.jsonl`
- `runs/` for generated agent-runtime prompt/script artifacts


## GitHub surfaces
- Codex sessions should use `gh` for issue comments, PR comments, and PR creation/update.
- `dani/github.py` and `dani/github_helper.py` remain PyGithub-backed internal surfaces for dani runtime logic.
