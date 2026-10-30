"""Three-tier persistent memory, Gemini-Task checklist state machine, and SQLite session store."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from typing import Any, Dict, List, Optional


DEFAULT_SOUL = """# SOUL.md — Core Agent Identity & Operating Invariants
You are Ouroboros, a recursive autonomous manager agent living on a dedicated server.
1. Act as a chief-of-staff and builder: decompose complex goals into verifiable milestones in TASK.md before executing.
2. Prefer delegating isolated sub-tasks to specialist skills or git-worktree sub-agents rather than polluting the root context.
3. Never fabricate command outputs, test results, or URLs. Verify every mutation with a deterministic check.
4. Respect Sentinel Tier-2 boundaries: halt and request human confirmation before modifying sensitive auth, firewall, or governance files.
"""


class MemoryAndStateStore:
    """Manages SOUL.md, MEMORY.md, TASK.md, and SQLite WAL session persistence."""

    def __init__(self, workspace_root: pathlib.Path) -> None:
        self.workspace_root = workspace_root
        self.soul_path = workspace_root / "SOUL.md"
        self.memory_path = workspace_root / "MEMORY.md"
        self.task_path = workspace_root / "planner" / "TASK.md"
        self.db_path = workspace_root / "state.db"
        self._init_files_and_db()

    def _init_files_and_db(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.task_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.soul_path.exists():
            self.soul_path.write_text(DEFAULT_SOUL, encoding="utf-8")
        if not self.memory_path.exists():
            self.memory_path.write_text(
                "# MEMORY.md — Durable Server & Project Facts\n", encoding="utf-8"
            )
        if not self.task_path.exists():
            self.task_path.write_text(
                "# TASK.md — Active Milestone Checklist\n- [ ] Awaiting task initialization\n",
                encoding="utf-8",
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    parent_session_id TEXT,
                    created_at REAL,
                    updated_at REAL,
                    messages_json TEXT,
                    status TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    timestamp REAL,
                    tool_name TEXT,
                    risk_tier TEXT,
                    args_json TEXT,
                    result_preview TEXT
                )
                """
            )

    def read_soul(self) -> str:
        return self.soul_path.read_text(encoding="utf-8")

    def read_memory(self) -> str:
        return self.memory_path.read_text(encoding="utf-8")

    def append_memory(self, fact: str) -> str:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        entry = f"- [{timestamp}] {fact.strip()}\n"
        with self.memory_path.open("a", encoding="utf-8") as f:
            f.write(entry)
        return f"Recorded to MEMORY.md: {entry.strip()}"

    def read_task_checklist(self) -> str:
        return self.task_path.read_text(encoding="utf-8")

    def update_task_checklist(self, markdown_content: str) -> str:
        self.task_path.write_text(markdown_content.strip() + "\n", encoding="utf-8")
        return f"Updated {self.task_path.name} ({len(markdown_content.splitlines())} lines)."

    def save_session(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        status: str = "active",
        parent_session_id: Optional[str] = None,
    ) -> None:
        now = time.time()
        payload = json.dumps(messages, ensure_ascii=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, parent_session_id, created_at, updated_at, messages_json, status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    messages_json = excluded.messages_json,
                    status = excluded.status
                """,
                (session_id, parent_session_id, now, now, payload, status),
            )

    def load_session(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT messages_json FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            return json.loads(row[0])

    def log_action_receipt(
        self,
        session_id: str,
        tool_name: str,
        risk_tier: str,
        args: Dict[str, Any],
        result_preview: str,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO action_receipts (session_id, timestamp, tool_name, risk_tier, args_json, result_preview)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    time.time(),
                    tool_name,
                    risk_tier,
                    json.dumps(args, ensure_ascii=False),
                    result_preview[:500],
                ),
            )
