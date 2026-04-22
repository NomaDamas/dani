from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from dani.session_bridge import MAX_PROMPT_CHARS, OmoSessionBridge


def create_opencode_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT, time_updated INTEGER)")
        conn.execute(
            "CREATE TABLE todo (session_id TEXT, content TEXT, status TEXT, position INTEGER, time_updated INTEGER)"
        )
        conn.execute("CREATE TABLE part (session_id TEXT, data TEXT, time_created INTEGER)")


def test_session_bridge_loads_summary_from_opencode_db(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    create_opencode_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session (id, title, directory, time_updated) VALUES (?, ?, ?, ?)",
            ("ses_demo", "Ship the fallback flow", str(repo_path), 2),
        )
        conn.executemany(
            "INSERT INTO todo (session_id, content, status, position, time_updated) VALUES (?, ?, ?, ?, ?)",
            [
                ("ses_demo", "Add Claude limit detector", "completed", 1, 1),
                ("ses_demo", "Bridge prior OMO context", "pending", 2, 2),
                ("ses_demo", "Write fallback tests", "in_progress", 3, 3),
            ],
        )
        conn.executemany(
            "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
            [
                ("ses_demo", json.dumps({"type": "text", "text": "Recent decision about Gastown-style priming"}), 3),
                ("ses_demo", json.dumps({"type": "tool", "text": "ignored"}), 2),
                ("ses_demo", "<bad-json>", 1),
            ],
        )

    bridge = OmoSessionBridge(db_path=db_path)
    context = bridge.load(repo_path=repo_path)

    assert context is not None
    assert context.source_session_id == "ses_demo"
    assert context.note is None
    assert "Prior OMO context" in context.prompt_block
    assert "Ship the fallback flow" in context.prompt_block
    assert "Add Claude limit detector" in context.prompt_block
    assert "Bridge prior OMO context" in context.prompt_block
    assert "Recent decision about Gastown-style priming" in context.prompt_block


def test_session_bridge_reports_missing_db(tmp_path: Path) -> None:
    bridge = OmoSessionBridge(db_path=tmp_path / "missing.db")
    context = bridge.load(repo_path=tmp_path / "repo")

    assert context is not None
    assert context.prompt_block == ""
    assert context.note == "opencode_db_missing"


def test_session_bridge_reports_unreadable_db(tmp_path: Path) -> None:
    db_path = tmp_path / "broken.db"
    db_path.write_text("not-a-sqlite-db", encoding="utf-8")

    bridge = OmoSessionBridge(db_path=db_path)
    context = bridge.load(repo_path=tmp_path / "repo")

    assert context is not None
    assert context.prompt_block == ""
    assert context.note == "opencode_db_unreadable"


def test_session_bridge_reports_unavailable_session(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    create_opencode_db(db_path)

    bridge = OmoSessionBridge(db_path=db_path)
    context = bridge.load(repo_path=tmp_path / "repo")

    assert context is not None
    assert context.prompt_block == ""
    assert context.note == "opencode_session_unavailable"


def test_session_bridge_truncates_prompt_deterministically(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    create_opencode_db(db_path)

    long_text = "context " * 600
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session (id, title, directory, time_updated) VALUES (?, ?, ?, ?)",
            ("ses_long", "Long context", str(repo_path), 1),
        )
        for index in range(8):
            conn.execute(
                "INSERT INTO part (session_id, data, time_created) VALUES (?, ?, ?)",
                ("ses_long", json.dumps({"type": "text", "text": f"{index} {long_text}"}), 100 - index),
            )

    bridge = OmoSessionBridge(db_path=db_path)
    context = bridge.load(repo_path=repo_path)

    assert context is not None
    assert len(context.prompt_block) <= MAX_PROMPT_CHARS
    assert context.prompt_block.endswith("…")
