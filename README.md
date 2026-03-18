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


## GitHub helper for agents
Agents should use the bundled PyGithub helper instead of `gh` subprocess calls:

```bash
python /absolute/path/to/dani/github_helper.py issue-comment --repo owner/name --issue 123 --body-file comment.md
python /absolute/path/to/dani/github_helper.py pr-comment --repo owner/name --pr 456 --body-file review.md
python /absolute/path/to/dani/github_helper.py ensure-pr --repo owner/name --head feature/#123 --base dev --title "Feature/#123" --body-file pr-body.md
```
