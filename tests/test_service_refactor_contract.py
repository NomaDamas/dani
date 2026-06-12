from datetime import timedelta

import dani.service as service
from dani import service_policy


def test_dani_service_policy_constants_are_extracted_and_reexported() -> None:
    assert timedelta(days=365) == service.MIN_EXTERNAL_CONTRIBUTOR_ACCOUNT_AGE
    assert service.MIN_EXTERNAL_CONTRIBUTOR_ACCOUNT_AGE == service_policy.MIN_EXTERNAL_CONTRIBUTOR_ACCOUNT_AGE
    assert service.RETRY_BACKOFF_SECONDS == service_policy.RETRY_BACKOFF_SECONDS
    assert service.ISSUE_COMMENT_RECOVERY_STAGES == service_policy.ISSUE_COMMENT_RECOVERY_STAGES
