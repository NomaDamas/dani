from dani.webhook import normalize_event


def test_normalize_pull_request_synchronize_event() -> None:
    event = normalize_event(
        "pull_request",
        {
            "action": "synchronize",
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "contributor"},
            "pull_request": {
                "number": 21,
                "title": "Feature/#21",
                "body": "Implements #21",
                "base": {"ref": "dev"},
                "head": {"ref": "feature/#21", "sha": "abc123"},
            },
        },
        delivery_id="delivery-1",
    )

    assert event is not None
    assert event.kind == "pull_request_opened"
    assert event.number == 21
    assert event.base_branch == "dev"
    assert event.head_branch == "feature/#21"
    assert event.commit_sha == "abc123"
    assert event.delivery_id == "delivery-1"
    assert event.is_pull_request is True


def test_normalize_pull_request_review_requested_event() -> None:
    event = normalize_event(
        "pull_request",
        {
            "action": "review_requested",
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "contributor"},
            "pull_request": {
                "number": 21,
                "title": "Feature/#21",
                "body": "Implements #21",
                "base": {"ref": "dev"},
                "head": {"ref": "feature/#21", "sha": "def456"},
            },
            "requested_reviewer": {"login": "maintainer"},
        },
    )

    assert event is not None
    assert event.kind == "pull_request_opened"
    assert event.number == 21
    assert event.base_branch == "dev"
    assert event.head_branch == "feature/#21"
    assert event.commit_sha == "def456"
    assert event.is_pull_request is True


def test_normalized_event_state_fields_default_to_none() -> None:
    from dani.models import NormalizedEvent

    event = NormalizedEvent(
        kind="issue_opened",
        repo_full_name="acme/demo",
        action="opened",
        number=1,
        actor_login="human",
        payload={},
    )

    assert event.issue_state is None
    assert event.pr_state is None
    assert event.pr_merged is None
    assert event.actor_type is None


def test_normalize_issue_opened_propagates_issue_state_and_actor_type() -> None:
    event = normalize_event(
        "issues",
        {
            "action": "opened",
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "human", "type": "User"},
            "issue": {"number": 42, "title": "t", "body": "b", "state": "open"},
        },
    )

    assert event is not None
    assert event.kind == "issue_opened"
    assert event.issue_state == "open"
    assert event.actor_type == "User"
    assert event.pr_state is None
    assert event.pr_merged is None


def test_normalize_issue_comment_propagates_issue_state_closed_and_actor_type() -> None:
    event = normalize_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "danibot", "type": "Bot"},
            "issue": {"number": 11, "state": "closed", "title": "t"},
            "comment": {"body": "still here?"},
        },
    )

    assert event is not None
    assert event.kind == "issue_comment"
    assert event.issue_state == "closed"
    assert event.actor_type == "Bot"


def test_normalize_pull_request_opened_propagates_pr_state_and_merged_false() -> None:
    event = normalize_event(
        "pull_request",
        {
            "action": "opened",
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "human", "type": "User"},
            "pull_request": {
                "number": 21,
                "title": "x",
                "body": "y",
                "state": "open",
                "merged": False,
                "base": {"ref": "dev"},
                "head": {"ref": "f/#21", "sha": "abc"},
            },
        },
    )

    assert event is not None
    assert event.pr_state == "open"
    assert event.pr_merged is False
    assert event.actor_type == "User"


def test_normalize_pull_request_review_comment_propagates_pr_state() -> None:
    event = normalize_event(
        "pull_request_review_comment",
        {
            "action": "created",
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "human", "type": "User"},
            "pull_request": {
                "number": 30,
                "title": "x",
                "body": "y",
                "state": "open",
                "merged": False,
                "base": {"ref": "dev"},
                "head": {"ref": "f/#30", "sha": "deadbeef"},
            },
            "comment": {"body": "looks good"},
        },
    )

    assert event is not None
    assert event.kind == "pull_request_comment"
    assert event.pr_state == "open"
    assert event.pr_merged is False
    assert event.actor_type == "User"


def test_normalize_pull_request_closed_merged_event() -> None:
    event = normalize_event(
        "pull_request",
        {
            "action": "closed",
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "danibot", "type": "Bot"},
            "pull_request": {
                "number": 70,
                "title": "Feature/#70",
                "body": "Implements #58",
                "state": "closed",
                "merged": True,
                "base": {"ref": "dev"},
                "head": {"ref": "feature/#70", "sha": "deadbeef"},
            },
        },
        delivery_id="d-70",
    )

    assert event is not None
    assert event.kind == "pull_request_closed"
    assert event.action == "closed"
    assert event.pr_state == "closed"
    assert event.pr_merged is True
    assert event.actor_type == "Bot"
    assert event.commit_sha == "deadbeef"
    assert event.is_pull_request is True


def test_normalize_pull_request_closed_unmerged_event() -> None:
    event = normalize_event(
        "pull_request",
        {
            "action": "closed",
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "human", "type": "User"},
            "pull_request": {
                "number": 71,
                "title": "Feature/#71",
                "body": "Implements #59",
                "state": "closed",
                "merged": False,
                "base": {"ref": "dev"},
                "head": {"ref": "feature/#71", "sha": "x"},
            },
        },
    )

    assert event is not None
    assert event.kind == "pull_request_closed"
    assert event.pr_state == "closed"
    assert event.pr_merged is False
