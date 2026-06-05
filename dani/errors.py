from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

SESSION_LIMIT_RETRY_AFTER = timedelta(hours=5)
WEEKLY_LIMIT_RETRY_AFTER = timedelta(days=7)

TRANSIENT_CAPACITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Selected model is at capacity", re.IGNORECASE),
    re.compile(r"model is currently overloaded", re.IGNORECASE),
]

CLAUDE_WEEKLY_LIMIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Opus weekly limit reached", re.IGNORECASE),
    re.compile(r"weekly limit reached", re.IGNORECASE),
]

CLAUDE_SESSION_LIMIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Claude usage limit reached", re.IGNORECASE),
    re.compile(r"Approaching usage limit", re.IGNORECASE),
]

RESET_HINT_PATTERN = re.compile(r"reset(?:s)?\s+(?:at|on)\s+(?P<hint>[^\n.]+)", re.IGNORECASE)

ROLLOUT_MISSING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"thread/resume failed", re.IGNORECASE),
    re.compile(r"no rollout found", re.IGNORECASE),
]

# Patterns emitted when an opencode session resume request targets a deleted or
# expired session. Derived from the CLI source:
#   "Session not found: ..." and related error text surfaced via the
#   opencode event stream / stderr.
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


class ClaudeUsageLimitError(Exception):
    """Raised when Claude usage limits require rerouting away from OMO/Claude."""

    def __init__(
        self,
        message: str,
        pattern: str,
        limit_type: str,
        *,
        reset_hint: str | None = None,
        suggested_retry_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.pattern = pattern
        self.limit_type = limit_type
        self.reset_hint = reset_hint
        self.suggested_retry_at = suggested_retry_at


class RolloutMissingError(Exception):
    """Raised when a resume target rollout/session can no longer be found.

    This error is runtime-agnostic: the Codex runner raises it when the
    codex rollout file is gone; the OMO (opencode) runner raises it when
    opencode reports the prior session no longer exists.
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


def check_claude_usage_limit_error(stderr_text: str) -> None:
    """Raise ``ClaudeUsageLimitError`` if *stderr_text* contains a known Claude usage-limit signal."""
    observed_at = datetime.now(tz=timezone.utc)
    reset_hint = _extract_reset_hint(stderr_text)

    for pattern in CLAUDE_WEEKLY_LIMIT_PATTERNS:
        match = pattern.search(stderr_text)
        if match:
            raise ClaudeUsageLimitError(
                match.group(0),
                pattern.pattern,
                "weekly",
                reset_hint=reset_hint,
                suggested_retry_at=(observed_at + WEEKLY_LIMIT_RETRY_AFTER).isoformat(),
            )

    for pattern in CLAUDE_SESSION_LIMIT_PATTERNS:
        match = pattern.search(stderr_text)
        if match:
            raise ClaudeUsageLimitError(
                match.group(0),
                pattern.pattern,
                "session_window",
                reset_hint=reset_hint,
                suggested_retry_at=(observed_at + SESSION_LIMIT_RETRY_AFTER).isoformat(),
            )


def check_rollout_missing_error(stderr_text: str) -> None:
    """Raise ``RolloutMissingError`` if *stderr_text* matches known Codex missing-rollout patterns."""
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


def _extract_reset_hint(text: str) -> str | None:
    match = RESET_HINT_PATTERN.search(text)
    if match is None:
        return None
    hint = match.group("hint").strip()
    return hint or None
