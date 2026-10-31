"""Three-tier persistent memory, Gemini-Task checklist state machine, and pooled SQLite WAL store (v0.4.0)."""

from __future__ import annotations

import copy
import json
import pathlib
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from ouroboros.cache import BoundedLRUCache, GLOBAL_FILE_CACHE


DEFAULT_SOUL = """# SOUL.md — Core Agent Identity & Operating Invariants
You are Ouroboros, a recursive autonomous manager agent living on a dedicated server.
1. Act as a chief-of-staff and builder: decompose complex goals into verifiable milestones in TASK.md before executing.
2. Prefer delegating isolated sub-tasks to specialist skills or isolated sub-agents rather than polluting the root context.
3. Never fabricate command outputs, test results, or URLs. Verify every mutation with a deterministic check.
4. Respect Sentinel Tier-2 boundaries: halt and request human confirmation before modifying sensitive auth, firewall, or governance files.
"""


class MemoryAndStateStore:
    """Manages SOUL.md, MEMORY.md, TASK.md, and pooled SQLite WAL session + key-value persistence."""

    def __init__(self, workspace_root: pathlib.Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.soul_path = self.workspace_root / "SOUL.md"
        self.memory_path = self.workspace_root / "MEMORY.md"
        self.task_path = self.workspace_root / "planner" / "TASK.md"
        self.db_path = self.workspace_root / "state.db"
        self._write_lock = threading.RLock()
        self._tls = threading.local()
        self._connections: List[sqlite3.Connection] = []
        self._kv_cache = BoundedLRUCache(max_size=512, ttl_seconds=3600.0)
        self._session_cache = BoundedLRUCache(max_size=128, ttl_seconds=1800.0)
        self._file_cache = GLOBAL_FILE_CACHE
        self._init_files_and_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Returns a thread-local persistent SQLite connection tuned for high-concurrency WAL access."""
        conn: Optional[sqlite3.Connection] = getattr(self._tls, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA cache_size=-64000;")
        conn.execute("PRAGMA mmap_size=268435456;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=5000;")
        self._tls.conn = conn
        with self._write_lock:
            self._connections.append(conn)
        return conn

    def checkpoint_and_close_all(self) -> None:
        """Flushes SQLite WAL to disk cleanly before an `os.execv` binary re-exec."""
        with self._write_lock:
            try:
                conn = self._get_conn()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.commit()
            except Exception:
                pass
            for c in list(self._connections):
                try:
                    c.close()
                except Exception:
                    pass
            self._connections.clear()
            self._tls.conn = None

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
        with self._write_lock:
            conn = self._get_conn()
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_receipts_session ON action_receipts(session_id, timestamp);"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value_json TEXT,
                    updated_at REAL
                )
                """
            )
            conn.commit()

    def set_kv(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        now = time.time()
        with self._write_lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO kv_store (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, payload, now),
            )
            conn.commit()
            self._kv_cache.put(key, copy.deepcopy(value))

    def get_kv(self, key: str, default: Any = None) -> Any:
        hit, cached_val = self._kv_cache.get(key)
        if hit:
            return copy.deepcopy(cached_val)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value_json FROM kv_store WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        val = json.loads(row[0])
        self._kv_cache.put(key, copy.deepcopy(val))
        return val

    def read_soul(self) -> str:
        return self._file_cache.read_text(self.soul_path, DEFAULT_SOUL)

    def read_memory(self) -> str:
        return self._file_cache.read_text(self.memory_path, "")

    def append_memory(self, fact: str) -> str:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        entry = f"- [{timestamp}] {fact.strip()}\n"
        with self._write_lock:
            with self.memory_path.open("a", encoding="utf-8") as f:
                f.write(entry)
            self._file_cache.invalidate(self.memory_path)
        return f"Recorded to MEMORY.md: {entry.strip()}"

    def read_task_checklist(self) -> str:
        return self._file_cache.read_text(self.task_path, "")

    def update_task_checklist(self, markdown_content: str) -> str:
        with self._write_lock:
            self.task_path.write_text(markdown_content.strip() + "\n", encoding="utf-8")
            self._file_cache.invalidate(self.task_path)
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
        with self._write_lock:
            conn = self._get_conn()
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
            conn.commit()
            self._session_cache.put(session_id, copy.deepcopy(messages))

    def load_session(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        hit, cached = self._session_cache.get(session_id)
        if hit:
            return copy.deepcopy(cached)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT messages_json FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        loaded = json.loads(row[0])
        self._session_cache.put(session_id, copy.deepcopy(loaded))
        return loaded

    def log_action_receipt(
        self,
        session_id: str,
        tool_name: str,
        risk_tier: str,
        args: Dict[str, Any],
        result_preview: str,
    ) -> None:
        with self._write_lock:
            conn = self._get_conn()
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
            conn.commit()

    def cache_metrics(self) -> Dict[str, Any]:
        return {
            "file_cache": self._file_cache.stats.to_dict(),
            "kv_lru_cache": self._kv_cache.stats.to_dict(),
            "session_lru_cache": self._session_cache.stats.to_dict(),
        }
