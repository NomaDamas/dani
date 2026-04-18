# Issue 33 verification log

This document records the development-time evidence that Issue 33 was resolved
according to the user's process constraints (no global install, no pm2
redeploy) and that the new OMO (Oh-My-OpenAgents) backend actually works
end-to-end against a real `opencode` subprocess.

## Process constraints

| Constraint | Evidence |
|---|---|
| No `uv tool install` during development | All commands used `uv run ...` against the local workspace only. No `uv tool install` was invoked. `git grep "uv tool install"` returns no matches in project-owned files; matches only come from vendored `.venv/**/METADATA` files unrelated to this work. |
| No pm2 redeploy | No changes to any pm2 config (none exists in repo) and no `pm2 start` / `pm2 reload` was invoked during development. `git grep pm2` returns no matches. |
| Local version only | `dani` is a uv-managed editable install in `.venv`. Verified via `uv run python -c "import dani, sys; print(dani.__file__)"` pointing inside the repo's working tree, not `~/.local/share/uv/tools/...`. |

## Live OMO subprocess runs (requirement 4)

Three reproducible smoke scripts under `scripts/dev/omo_smoke/` spawn real
`opencode run` subprocesses and inspect the logs. They are checked into the
repo so future reviewers can re-run them. An equivalent pytest file at
`tests/test_omo_live.py` is opt-in (`DANI_OMO_LIVE=1 uv run pytest tests/test_omo_live.py`)
and is skipped by default because it consumes model tokens.

| Script | Proves | Sample captured session id |
|---|---|---|
| `smoke_launch.py` | `OmoRunner.launch` spawns opencode, gets back `{"type":"text","text":"dani omo smoke ok"}`, sessionID captured. | `ses_25e9164e7ffeleSyF3UGalrfuo` |
| `smoke_resume.py` | `OmoRunner.resume` runs `opencode run --session <id>` and opencode **remembers** a keyword planted in phase 1 (`PURPLE_HIPPO_9182`). | `ses_25e99ee42ffeZiayJpmLNjDbBQ` |
| `smoke_ultrawork.py` | `OmoRunner` unconditionally prefixes `prompt.txt` with `"ultrawork\n\n"` and real opencode accepts it. | `ses_25e919f1cffe7LPKpAR04SCyNQ` |

All three scripts passed their assertions when executed locally with
`uv run python scripts/dev/omo_smoke/<name>.py` against `opencode` 1.4.11 on
macOS (Darwin), with opencode data dir at `~/.local/share/opencode/opencode.db`.
Session IDs above are pulled from actual run logs and are specific to that
environment — they will differ on every execution.

## Unit / integration test coverage

- 154 tests pass (`uv run pytest -q`), up from 125 before this work.
- New tests in `tests/test_omo_runner.py` cover:
  - Factory selection (both `omx` and `omo`; ultrawork flag forwarded only to omo; ultrawork+omx rejected with `ValueError`).
  - `opencode run` and `opencode run --session <id>` script construction.
  - JSONL `sessionID` parsing from stdout, including partial-line / malformed-line tolerance.
  - `get_session_id(runtime_handle)` post-wait recovery.
  - Prompt-file contents (raw and ultrawork-decorated, idempotency).
  - Error handling: `Session not found` (→ `RolloutMissingError`) and `model is at capacity` (→ `TransientCapacityError`).
  - Full `DaniService` integration paths:
    - `issue_request` launched through OMO → session persisted → job completed.
    - `issue_opened` → persisted sessionID → `issue_comment` → OmoRunner.resume with exact `--session <id>` flag.
    - Follow-up resume failing with `Session not found` stderr → job marked failed with `error=rollout_missing` → `stage=session_lost` warning comment posted.
    - Late sessionID emission (opencode emits id AFTER launch's pre-wait poll times out): service re-queries `get_session_id` post-wait and updates storage.
- New tests in `tests/test_omo_live.py` cover the same surface with **real**
  opencode subprocess execution, opt-in via `DANI_OMO_LIVE=1`.

## Backward compatibility

- No field renames in persisted records (`SessionRecord.omx_session_id`,
  `JobRecord.metadata["omx_session_id"]`, `storage.find_latest_session(require_omx_session_id=...)`)
  — existing `~/.dani/sessions.json` and `jobs.json` stay readable.
- Default runtime is unchanged (`agent_runtime="omx"`); OMO is strictly opt-in
  via `DANI_AGENT_RUNTIME=omo`.
- All 125 pre-existing tests still pass without modification.
- `FakeOmxRunner` test double unchanged; satisfies the new `AgentRunner`
  protocol structurally.
