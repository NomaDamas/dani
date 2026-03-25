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


def normalize_event(event_name: str, payload: dict[str, Any]) -> NormalizedEvent | None:
    repo = payload.get("repository") or {}
    repo_full_name = repo.get("full_name")
    if not repo_full_name:
        return None

    action = payload.get("action", "")
    if event_name == "issues" and action == "opened":
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
        )

    if event_name == "issue_comment" and action == "created":
        issue = payload["issue"]
        is_pr = bool(issue.get("pull_request"))
        return NormalizedEvent(
            kind="pull_request_comment" if is_pr else "issue_comment",
            repo_full_name=repo_full_name,
            action=action,
            number=issue["number"],
            actor_login=payload["sender"]["login"],
            payload=payload,
            body=payload["comment"].get("body"),
            title=issue.get("title"),
            is_pull_request=is_pr,
        )

    if event_name == "push":
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
            ref=ref,
            commit_sha=commit_sha,
        )

    if event_name == "pull_request" and action == "opened":
        pull_request = payload["pull_request"]
        return NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name=repo_full_name,
            action=action,
            number=pull_request["number"],
            actor_login=payload["sender"]["login"],
            payload=payload,
            body=pull_request.get("body"),
            title=pull_request.get("title"),
            base_branch=pull_request["base"]["ref"],
            head_branch=pull_request["head"]["ref"],
            is_pull_request=True,
        )

    if event_name == "pull_request_review_comment" and action == "created":
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
            is_pull_request=True,
        )

    return None


def parse_body(body: bytes) -> dict[str, Any]:
    return json.loads(body.decode("utf-8"))
