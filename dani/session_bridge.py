from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OPENCODE_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
MAX_SECTION_ITEMS = 3
MAX_SNIPPET_LENGTH = 280
MAX_PROMPT_CHARS = 1800


@dataclass(slots=True)
class BridgeContext:
    prompt_block: str
    source_runtime: str = "omo"
    source_session_id: str | None = None
    note: str | None = None


class OmoSessionBridge:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DEFAULT_OPENCODE_DB_PATH

    def load(self, *, repo_path: Path, session_id: str | None = None) -> BridgeContext | None:
        if not self.db_path.exists():
            return BridgeContext(prompt_block="", note="opencode_db_missing")

        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                session_row = self._select_session(conn, repo_path=repo_path, session_id=session_id)
                if session_row is None:
                    return BridgeContext(prompt_block="", note="opencode_session_unavailable")
                source_session_id = str(session_row["id"])
                session_title = _clean_text(session_row["title"])
                completed = self._load_todos(conn, source_session_id, status="completed")
                pending = self._load_todos(conn, source_session_id, status="pending")
                pending.extend(self._load_todos(conn, source_session_id, status="in_progress"))
                recent_text = self._load_recent_text(conn, source_session_id)
        except sqlite3.Error:
            return BridgeContext(prompt_block="", note="opencode_db_unreadable")

        lines = [
            "Prior OMO context (imported summary; not a native resume):",
            f"- Source session id: {source_session_id}",
        ]
        if session_title:
            lines.append(f"- Prior goal: {session_title}")
        if completed:
            lines.append("- Latest completed work:")
            lines.extend(f"  - {item}" for item in completed[:MAX_SECTION_ITEMS])
        if pending:
            lines.append("- Open thread:")
            lines.extend(f"  - {item}" for item in pending[:MAX_SECTION_ITEMS])
        if recent_text:
            lines.append("- Recent relevant context:")
            lines.extend(f"  - {item}" for item in recent_text[:MAX_SECTION_ITEMS])

        prompt_block = _truncate_block("\n".join(lines), MAX_PROMPT_CHARS)
        if prompt_block.strip() == "Prior OMO context (imported summary; not a native resume):":
            return BridgeContext(prompt_block="", source_session_id=source_session_id, note="opencode_context_empty")
        return BridgeContext(prompt_block=prompt_block, source_session_id=source_session_id)

    def _select_session(
        self, conn: sqlite3.Connection, *, repo_path: Path, session_id: str | None
    ) -> sqlite3.Row | None:
        if session_id:
            row = conn.execute(
                "SELECT id, title, directory FROM session WHERE id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is not None:
                return row
        return conn.execute(
            "SELECT id, title, directory FROM session WHERE directory = ? ORDER BY time_updated DESC LIMIT 1",
            (str(repo_path),),
        ).fetchone()

    def _load_todos(self, conn: sqlite3.Connection, session_id: str, *, status: str) -> list[str]:
        rows = conn.execute(
            "SELECT content FROM todo WHERE session_id = ? AND status = ? ORDER BY position ASC, time_updated DESC LIMIT 6",
            (session_id, status),
        ).fetchall()
        return [_clean_text(str(row["content"])) for row in rows if _clean_text(str(row["content"]))]

    def _load_recent_text(self, conn: sqlite3.Connection, session_id: str) -> list[str]:
        rows = conn.execute(
            "SELECT data FROM part WHERE session_id = ? ORDER BY time_created DESC LIMIT 40",
            (session_id,),
        ).fetchall()
        snippets: list[str] = []
        for row in rows:
            try:
                payload = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("type") != "text":
                continue
            text = _clean_text(str(payload.get("text") or ""))
            if not text:
                continue
            if text.startswith("<ultrawork-mode>"):
                continue
            if "ULTRAWORK MODE ENABLED!" in text:
                continue
            snippets.append(text)
            if len(snippets) >= 6:
                break
        return snippets


def _clean_text(text: str) -> str:
    stripped = " ".join(text.split())
    if len(stripped) > MAX_SNIPPET_LENGTH:
        return stripped[: MAX_SNIPPET_LENGTH - 1].rstrip() + "…"
    return stripped


def _truncate_block(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
