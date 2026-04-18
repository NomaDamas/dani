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
  or `opencode run --session <id>` (Oh-My-OpenAgents)
- Separate prompt templates in `dani/prompts.py`
- Workflows for:
  - issue request report
  - `/approve` implementation
  - 3 review rounds for agent-authored PRs
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
- `DANI_AGENT_ULTRAWORK` — set to `1` / `true` / `yes` / `on` to prefix every
  opencode prompt with the `ultrawork` keyword, which activates
  oh-my-openagents' ultrawork loop mode. Only valid when
  `DANI_AGENT_RUNTIME=omo`; setting it with `omx` is rejected at startup.

## Codex/OMX trust prerequisite
Before dani can reliably launch or resume OMX/Codex sessions for a repository, that repository directory should be trusted by Codex at least once. In practice, run `omx exec 'hello'` or `codex exec 'hello'` once from the target repo and accept the trust prompt before using dani automation there. Otherwise a trust prompt can block session startup or resume.

## Oh-My-OpenAgents (opencode) prerequisite
When running with `DANI_AGENT_RUNTIME=omo`, dani launches sessions through
`opencode run --format json --dangerously-skip-permissions <prompt>` and resumes
them with `opencode run --session <id> ...`. Install `opencode` (the
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
