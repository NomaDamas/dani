# dani

Simple GitHub webhook -> agent automation loop. Supports two pluggable agent
runtimes: **Oh-My-Codex (`omx`, default)** and **Oh-My-OpenAgents (`omo`,
opt-in)**.

## What v1 includes
- Typer CLI
- FastAPI webhook server
- Registered repos only
- Repo-serial / cross-repo parallel job handling
- Pluggable agent runtime: non-interactive `omx exec` / `omx exec resume` (default)
  or HTTP-backed Oh-My-OpenAgents (`opencode serve`)
- Separate prompt templates in `dani/prompts.py`
- Workflows for:
  - issue request report
  - `/approve` implementation
  - 3 review rounds for agent-authored PRs
  - external contributor account-age eligibility checks
  - event-driven, duplicate-delivery-safe re-review for external contributor PRs
  - final verdict + auto-merge on APPROVE

## Environment
Required local tools (at least one depending on the selected runtime):
- `git`
- `omx` — required when `DANI_AGENT_RUNTIME=omx` (default)
- `opencode` — required when `DANI_AGENT_RUNTIME=omo`

Required environment variables:
- `DANI_WEBHOOK_SECRET`
- `DANI_GITHUB_TOKEN` (preferred) or `GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_PAT`

Optional environment variables:
- `DANI_AGENT_RUNTIME` — selects the agent backend. Accepted values:
  - `omx` / `oh-my-codex` / `codex` (default)
  - `omo` / `oh-my-openagents` / `oh-my-openagent` / `opencode`
- `DANI_AGENT_TIMEOUT_SECONDS` — overrides the per-job agent wait timeout in seconds.

Optional config file (`~/.dani/config.json` by default, or `<data-dir>/config.json`):

```json
{
  "agent_runtime": "omo",
  "agent_timeout_seconds": 3600
}
```

`agent_timeout_seconds` defaults to `3600`. The environment variable
`DANI_AGENT_TIMEOUT_SECONDS` takes precedence over the config file when set.

When `DANI_AGENT_RUNTIME=omo` is selected, dani automatically prefixes every
opencode prompt with the `ultrawork` keyword so oh-my-openagents' ultrawork
loop mode is always active, and runtime-specific prompt substitutions swap
codex-only shell commands (`$ralph`, `$code-review`) for their opencode
equivalents (ultrawork mode and the Momus-Plan-Critic subagent respectively).

By default, `omo` drives opencode through its long-lived **HTTP server**
(`opencode serve`) instead of one-shot `opencode run` subprocesses. Each
registered repository gets its own cached `opencode serve` process, lazily
spawned on first launch and reused for every subsequent session in that
repo. The HTTP backend keeps the agent loop alive inside the server even
when subagents (oh-my-openagents `task` calls) come and go, which avoids
the parent-process termination problem the subprocess backend hit when
subagents finished.

Optional `omo` runtime environment variables:
- `DANI_OPENCODE_SERVER_URL` — attach every repo to a single external
  opencode server (e.g. `http://127.0.0.1:4096`) instead of spawning local
  ones. Sessions are still scoped per repo via the `directory` query param.
- `DANI_OPENCODE_PERMISSION_RESPONSE` — `once` (default), `always`, or
  `reject`. Controls how dani responds to opencode `permission.updated`
  events automatically.
- `OPENCODE_SERVER_PASSWORD` — forwarded to the spawned server and used
  for HTTP basic auth on every request when set.

## Codex/OMX trust prerequisite
Before dani can reliably launch or resume OMX/Codex sessions for a repository, that repository directory should be trusted by Codex at least once. In practice, run `omx exec 'hello'` or `codex exec 'hello'` once from the target repo and accept the trust prompt before using dani automation there. Otherwise a trust prompt can block session startup or resume.

## Oh-My-OpenAgents (opencode) prerequisite
When running with `DANI_AGENT_RUNTIME=omo`, dani drives opencode through the
`opencode serve` HTTP backend by default: it spawns `opencode serve --port 0
--hostname 127.0.0.1 --print-logs` once per registered repository (with the
repo as cwd), then talks to that server over HTTP for session create, prompt
submission, completion via SSE, and resume. Install `opencode` (the
`oh-my-openagent` plugin is loaded automatically when configured in
`~/.config/opencode/opencode.json`) and make sure the target repository
directory is trusted at least once via `opencode run 'hello'` before pointing
dani at it.

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
| `binaries` | `git`, `gh`, plus runtime-specific `omx`/`codex` or `opencode` (skips opencode if `DANI_OPENCODE_SERVER_URL` is set) |
| `storage_files` | parse-ability of `registry.json`/`jobs.json`/`sessions.json`/`processed-events.json`/`terminal-targets.json`; tolerates one transient parse error per file plus an `events.jsonl` last-line append race |
| `registered_repos` | each registered repo's `local_path` is a git working tree and resolves both `main_branch` and `dev_branch` |
| `github_auth` | resolves the GitHub token, verifies it with the minimum `Github.get_rate_limit()` call, reports rate-limit headroom; never echoes the token, only its source env-var name |
| `server_health` | if `127.0.0.1:<port>` is listening, GET `/health` and compare to `{"status":"ok"}`; SKIP (not FAIL) when no local server |
| `stuck_jobs` | warns when active jobs (`queued`/`launched`/`retrying`/`recovering`) are older than `stuck_job_age_seconds` (default = `agent_timeout_seconds`) |
| `stuck_sessions` | FAILs on confirmed A-class drift (session `launched` while linked job is terminal — re-validated with a 100ms re-snapshot and a 60s grace window); WARNs on long-running launches and orphans |
| `disk_usage` | size of each storage file plus a soft-deadlined walk of `runs/`; thresholds for warn/fail per target |
| `backup_files` | accumulated `*.bak.*` files in `data_dir`: count / oldest age / total bytes |
| `process_sprawl` | counts alive `omx exec` / `opencode serve` / `opencode run` processes; reports only PID/PPID/etime/classifier — never raw argv |

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
- `runs/` for generated agent-runtime prompt/script artifacts (omx or omo)


## GitHub surfaces
- OMX sessions should use `gh` for issue comments, PR comments, and PR creation/update.
- `dani/github.py` and `dani/github_helper.py` remain PyGithub-backed internal surfaces for dani runtime logic.
