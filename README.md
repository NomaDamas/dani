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
```

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
