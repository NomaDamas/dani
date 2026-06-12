from __future__ import annotations

from datetime import timedelta

MIN_EXTERNAL_CONTRIBUTOR_ACCOUNT_AGE = timedelta(days=365)
INELIGIBLE_EXTERNAL_PR_COMMENT = (
    "Thanks for your interest in contributing. This pull request has been closed automatically because this "
    "repository only accepts pull requests from GitHub accounts that are at least one year old. If you would like "
    "to request this change or feature, please open an issue instead so maintainers can review it."
)

RETARGET_REQUEST_STAGE = "retarget_request"
ISSUE_REQUEST_RECOVERY_STAGE = "issue_request_recovery"
ISSUE_FOLLOWUP_RECOVERY_STAGE = "issue_followup_recovery"
ISSUE_COMMENT_RECOVERY_STAGES = {
    ISSUE_REQUEST_RECOVERY_STAGE: "issue_request",
    ISSUE_FOLLOWUP_RECOVERY_STAGE: "issue_followup",
}
ISSUE_COMMENT_MISSING_ERRORS = {
    "issue-request-comment-missing",
    "issue-followup-comment-missing",
}
MAX_COMMENT_RECOVERY_ATTEMPTS = 1

RETRY_BACKOFF_SECONDS: list[int] = [60, 180, 600]
