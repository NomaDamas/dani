from __future__ import annotations

import re

TRANSIENT_CAPACITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Selected model is at capacity", re.IGNORECASE),
    re.compile(r"model is currently overloaded", re.IGNORECASE),
]

ROLLOUT_MISSING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"thread/resume failed", re.IGNORECASE),
    re.compile(r"no rollout found", re.IGNORECASE),
]

# Patterns emitted by `opencode run --session ...` when the requested session
# has been deleted/expired. Derived from the CLI source:
#   "Session not found: ..." and related error text surfaced via the
#   opencode run JSON event stream / stderr.
OPENCODE_SESSION_MISSING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[Ss]ession not found", re.IGNORECASE),
    re.compile(r"[Ss]ession does not exist", re.IGNORECASE),
    re.compile(r"[Nn]o session with id", re.IGNORECASE),
]


class TransientCapacityError(Exception):
    """Raised when an agent session fails due to a transient model-capacity issue."""

    def __init__(self, message: str, pattern: str) -> None:
        super().__init__(message)
        self.pattern = pattern


class RolloutMissingError(Exception):
    """Raised when a resume target rollout/session can no longer be found.

    This error is runtime-agnostic: the OMX (codex) runner raises it when the
    codex rollout file is gone; the OMO (opencode) runner raises it when
    ``opencode run --session <id>`` reports the session no longer exists.
    """

    def __init__(self, message: str, pattern: str) -> None:
        super().__init__(message)
        self.pattern = pattern


def check_transient_capacity_error(stderr_text: str) -> None:
    """Raise ``TransientCapacityError`` if *stderr_text* contains a known capacity pattern."""
    for pattern in TRANSIENT_CAPACITY_PATTERNS:
        match = pattern.search(stderr_text)
        if match:
            raise TransientCapacityError(match.group(0), pattern.pattern)


def check_rollout_missing_error(stderr_text: str) -> None:
    """Raise ``RolloutMissingError`` if *stderr_text* matches known OMX missing-rollout patterns."""
    for pattern in ROLLOUT_MISSING_PATTERNS:
        match = pattern.search(stderr_text)
        if match:
            raise RolloutMissingError(stderr_text.strip() or match.group(0), pattern.pattern)


def check_opencode_session_missing_error(stderr_text: str) -> None:
    """Raise ``RolloutMissingError`` if *stderr_text* matches known opencode session-missing patterns."""
    for pattern in OPENCODE_SESSION_MISSING_PATTERNS:
        match = pattern.search(stderr_text)
        if match:
            raise RolloutMissingError(stderr_text.strip() or match.group(0), pattern.pattern)
