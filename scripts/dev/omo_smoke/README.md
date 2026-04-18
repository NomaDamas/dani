# OMO (Oh-My-OpenAgents) live smoke scripts

These three scripts exercise the `OmoRunner` against a **real** `opencode`
binary. They are committed here so anyone verifying Issue 33 (`omo` backend
support) can reproduce the same end-to-end evidence locally.

Unlike the unit/integration tests under `tests/test_omo_runner.py`, these
scripts do **not** monkeypatch `subprocess.Popen`. They spawn real `opencode run`
processes, consume real model tokens, and assert on real CLI output.

## Prerequisites

- `opencode` binary on `PATH` (install via `curl -fsSL https://opencode.ai/install | bash`,
  `npm install -g oh-my-opencode`, `brew install opencode`, etc.).
- An opencode model provider configured (`opencode auth login` once).
- `uv` available for running the scripts inside the project's virtualenv.

## Running

Each script creates its own throwaway workspace under `/tmp/dani-omo-smoke/`
(or the path the script defines). No global state, no writes to your repos.

```sh
# from the dani repo root, using the local (uninstalled) version:
uv run python scripts/dev/omo_smoke/smoke_launch.py
uv run python scripts/dev/omo_smoke/smoke_resume.py
uv run python scripts/dev/omo_smoke/smoke_ultrawork.py
```

## What each script proves

| Script | Proves |
|--------|--------|
| `smoke_launch.py` | `OmoRunner.launch` actually spawns `opencode run --format json --dangerously-skip-permissions ...`, the model replies, and `_capture_session_id` extracts a valid `ses_...` id from the JSONL stream. |
| `smoke_resume.py` | `OmoRunner.resume` runs `opencode run --session <id> ...` and opencode **continues** the prior conversation (recalls a keyword planted in phase 1). |
| `smoke_ultrawork.py` | When `OmoRunner(ultrawork=True)`, the `prompt.txt` gets `"ultrawork\n\n"` prepended and opencode accepts it. |

## Why these are not part of `pytest` by default

They take 10–60 seconds each and cost model tokens. The equivalent pytest
integration file is `tests/test_omo_live.py`, which is skipped unless you set
`DANI_OMO_LIVE=1` and have `opencode` on `PATH`.
