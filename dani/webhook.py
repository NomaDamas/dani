from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from dani.models import NormalizedEvent


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _actor_type(payload: dict[str, Any]) -> str | None:
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return None
    type_value = sender.get("type")
    if isinstance(type_value, str) and type_value:
        return type_value
    return None


def _coerce_state(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _coerce_merged(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _normalize_issue_opened(
    payload: dict[str, Any], *, repo_full_name: str, action: str, delivery_id: str | None
) -> NormalizedEvent:
    issue = payload["issue"]
    return NormalizedEvent(
        kind="issue_opened",
        repo_full_name=repo_full_name,
        action=action,
        number=issue["number"],
        actor_login=payload["sender"]["login"],
        payload=payload,
        body=issue.get("body"),
        title=issue.get("title"),
        delivery_id=delivery_id,
        issue_state=_coerce_state(issue.get("state")),
        actor_type=_actor_type(payload),
    )


def _normalize_issue_comment(
    payload: dict[str, Any], *, repo_full_name: str, action: str, delivery_id: str | None
) -> NormalizedEvent:
    issue = payload["issue"]
    is_pr = bool(issue.get("pull_request"))
    pr_marker = issue.get("pull_request") if is_pr else None
    pr_merged: bool | None = None
    if isinstance(pr_marker, dict) and pr_marker.get("merged_at") is not None:
        pr_merged = True
    issue_state = _coerce_state(issue.get("state"))
    return NormalizedEvent(
        kind="pull_request_comment" if is_pr else "issue_comment",
        repo_full_name=repo_full_name,
        action=action,
        number=issue["number"],
        actor_login=payload["sender"]["login"],
        payload=payload,
        body=payload["comment"].get("body"),
        title=issue.get("title"),
        delivery_id=delivery_id,
        is_pull_request=is_pr,
        issue_state=issue_state,
        pr_state=issue_state if is_pr else None,
        pr_merged=pr_merged,
        actor_type=_actor_type(payload),
    )


def _normalize_branch_push(
    payload: dict[str, Any], *, repo_full_name: str, delivery_id: str | None
) -> NormalizedEvent | None:
    ref = payload.get("ref")
    commit_sha = payload.get("after")
    if not ref or not commit_sha or payload.get("deleted"):
        return None
    return NormalizedEvent(
        kind="branch_push",
        repo_full_name=repo_full_name,
        action="push",
        number=0,
        actor_login=payload["sender"]["login"],
        payload=payload,
        delivery_id=delivery_id,
        ref=ref,
        commit_sha=commit_sha,
        actor_type=_actor_type(payload),
    )


def _normalize_pull_request(
    payload: dict[str, Any],
    *,
    repo_full_name: str,
    action: str,
    kind: str,
    delivery_id: str | None,
) -> NormalizedEvent:
    pull_request = payload["pull_request"]
    return NormalizedEvent(
        kind=kind,
        repo_full_name=repo_full_name,
        action=action,
        number=pull_request["number"],
        actor_login=payload["sender"]["login"],
        payload=payload,
        body=pull_request.get("body"),
        title=pull_request.get("title"),
        base_branch=pull_request["base"]["ref"],
        head_branch=pull_request["head"]["ref"],
        delivery_id=delivery_id,
        commit_sha=pull_request["head"].get("sha"),
        is_pull_request=True,
        pr_state=_coerce_state(pull_request.get("state")),
        pr_merged=_coerce_merged(pull_request.get("merged")),
        actor_type=_actor_type(payload),
    )


def _normalize_pull_request_review_comment(
    payload: dict[str, Any], *, repo_full_name: str, action: str, delivery_id: str | None
) -> NormalizedEvent:
    pull_request = payload["pull_request"]
    return NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name=repo_full_name,
        action=action,
        number=pull_request["number"],
        actor_login=payload["sender"]["login"],
        payload=payload,
        body=payload["comment"].get("body"),
        title=pull_request.get("title"),
        base_branch=pull_request["base"]["ref"],
        head_branch=pull_request["head"]["ref"],
        delivery_id=delivery_id,
        commit_sha=pull_request["head"].get("sha"),
        is_pull_request=True,
        pr_state=_coerce_state(pull_request.get("state")),
        pr_merged=_coerce_merged(pull_request.get("merged")),
        actor_type=_actor_type(payload),
    )


_PULL_REQUEST_OPEN_ACTIONS = frozenset({
    "opened",
    "synchronize",
    "reopened",
    "ready_for_review",
    "review_requested",
})


def normalize_event(
    event_name: str, payload: dict[str, Any], *, delivery_id: str | None = None
) -> NormalizedEvent | None:
    repo = payload.get("repository") or {}
    repo_full_name = repo.get("full_name")
    if not repo_full_name:
        return None

    action = payload.get("action", "")

    if event_name == "issues" and action == "opened":
        return _normalize_issue_opened(payload, repo_full_name=repo_full_name, action=action, delivery_id=delivery_id)
    if event_name == "issue_comment" and action == "created":
        return _normalize_issue_comment(payload, repo_full_name=repo_full_name, action=action, delivery_id=delivery_id)
    if event_name == "push":
        return _normalize_branch_push(payload, repo_full_name=repo_full_name, delivery_id=delivery_id)
    if event_name == "pull_request" and action in _PULL_REQUEST_OPEN_ACTIONS:
        return _normalize_pull_request(
            payload,
            repo_full_name=repo_full_name,
            action=action,
            kind="pull_request_opened",
            delivery_id=delivery_id,
        )
    if event_name == "pull_request" and action == "closed":
        return _normalize_pull_request(
            payload,
            repo_full_name=repo_full_name,
            action=action,
            kind="pull_request_closed",
            delivery_id=delivery_id,
        )
    if event_name == "pull_request_review_comment" and action == "created":
        return _normalize_pull_request_review_comment(
            payload, repo_full_name=repo_full_name, action=action, delivery_id=delivery_id
        )

    return None


def parse_body(body: bytes) -> dict[str, Any]:
    return json.loads(body.decode("utf-8"))
