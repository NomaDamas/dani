from __future__ import annotations

import re

SIGNATURE_PATTERN = re.compile(r"<!--\s*(?:dani|DANI):\s*(?P<body>[^>]+)\s*-->")


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
    fields: dict[str, str] = {}
    raw_body = match.group("body")
    parts = raw_body.split(";") if ";" in raw_body else raw_body.split()
    for item in parts:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields or None


def has_agent_signature(text: str | None) -> bool:
    return parse_signature(text) is not None
