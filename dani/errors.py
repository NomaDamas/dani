from __future__ import annotations

import re

TRANSIENT_CAPACITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Selected model is at capacity", re.IGNORECASE),
    re.compile(r"model is currently overloaded", re.IGNORECASE),
]


class TransientCapacityError(Exception):
    """Raised when an OMX session fails due to a transient model-capacity issue."""

    def __init__(self, message: str, pattern: str) -> None:
        super().__init__(message)
        self.pattern = pattern


def check_transient_capacity_error(stderr_text: str) -> None:
    """Raise ``TransientCapacityError`` if *stderr_text* contains a known capacity pattern."""
    for pattern in TRANSIENT_CAPACITY_PATTERNS:
        match = pattern.search(stderr_text)
        if match:
            raise TransientCapacityError(match.group(0), pattern.pattern)
