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
- `gh`
- `git`
- `omx`
- `tmux`

Required environment variable:
- `DANI_WEBHOOK_SECRET`

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
