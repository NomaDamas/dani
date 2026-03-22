# dani

Simple GitHub webhook -> OMX automation loop.

## What v1 includes
- Typer CLI
- FastAPI webhook server
- Registered repos only
- Repo-serial / cross-repo parallel job handling
- `omx --madmax` tmux launches
- Separate prompt templates in `dani/prompts.py`
- Workflows for:
  - issue request report
  - `/approve` implementation
  - 3 review rounds for PRs
  - final verdict + auto-merge on APPROVE

## Environment
Required local tools:
- `git`
- `omx`
- `tmux`

Required environment variables:
- `DANI_WEBHOOK_SECRET`
- `DANI_GITHUB_TOKEN` (preferred) or `GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_PAT`

## Codex/OMX trust prerequisite
Before dani can reliably launch or resume OMX/Codex sessions for a repository, that repository directory should be trusted by Codex at least once. In practice, run `omx` or `codex` once from the target repo and accept the trust prompt before using dani automation there. Otherwise a trust prompt can block session startup or resume.

## CLI
```bash
dani register-repo owner/name /absolute/path/to/repo
dani serve --data-dir .dani
dani bootstrap owner/name
dani show-state
```

## Persistence
State is stored under `.dani/` by default:
- `registry.json`
- `jobs.json`
- `sessions.json`
- `events.jsonl`
- `runs/` for generated OMX prompt/script artifacts


## GitHub surfaces
- OMX sessions should use `gh` for issue comments, PR comments, and PR creation/update.
- `dani/github.py` and `dani/github_helper.py` remain PyGithub-backed internal surfaces for dani runtime logic.
