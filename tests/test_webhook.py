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
