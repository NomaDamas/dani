from __future__ import annotations

import re

SIGNATURE_PATTERN = re.compile(r"<!--\s*(?:dani|DANI):\s*(?P<body>[^>]+)\s*-->")
IGNORE_COMMAND_PATTERN = re.compile(r"(?mi)(?:^|\s)/dani\s+ignore(?:\s|$)")


def _parse_signature_body(raw_body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    parts = raw_body.split(";") if ";" in raw_body else raw_body.split()
    for item in parts:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def build_signature(**fields: object) -> str:
    body = ";".join(f"{key}={value}" for key, value in fields.items())
    return f"<!-- dani:{body} -->"


def render_signature(**fields: object) -> str:
    body = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"<!-- DANI: {body} -->"


def parse_signature(text: str | None) -> dict[str, str] | None:
    if not text:
        return None
    match = SIGNATURE_PATTERN.search(text)
    if not match:
        return None
    fields = _parse_signature_body(match.group("body"))
    return fields or None


def parse_agent_signature(text: str | None) -> dict[str, str] | None:
    fields = parse_signature(text)
    if fields is None or fields.get("stage", "").lower() == "ignore":
        return None
    return fields


def has_agent_signature(text: str | None) -> bool:
    return parse_agent_signature(text) is not None


def has_ignore_signature(text: str | None) -> bool:
    if not text:
        return False
    for match in SIGNATURE_PATTERN.finditer(text):
        fields = _parse_signature_body(match.group("body"))
        if fields.get("stage", "").lower() == "ignore":
            return True
    return False


def is_opt_out_comment(text: str | None) -> bool:
    return has_ignore_signature(text) or bool(text and IGNORE_COMMAND_PATTERN.search(text))
