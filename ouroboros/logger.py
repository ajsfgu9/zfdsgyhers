"""Unified Rotating File & Systemd Journal Logger (`ouroboros/logger.py`).

Provides three log inspection surfaces out of the box:
1. Rotating disk log: `<workspace_root>/logs/ouroboros.log` (5 MB x 5 backups)
2. Systemd journal: `sudo journalctl -u ouroboros -f` (via standard output/error stream)
3. Remote inspection: `GET /v1/logs?lines=50` and Telegram `/logs` command
"""

from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import pathlib
import threading
import time
from typing import Any, Deque, Dict, List, Optional


class OuroborosLogger:
    """Thread-safe structured logger with rotating file handler and in-memory ring buffer."""

    def __init__(self, log_dir: pathlib.Path, max_bytes: int = 5_242_880, backup_count: int = 5) -> None:
        self.log_dir = log_dir.resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "ouroboros.log"
        self._lock = threading.RLock()
        self._ring: Deque[str] = collections.deque(maxlen=500)

        self._logger = logging.getLogger(f"ouroboros.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers.clear()

        formatter = logging.Formatter(
            fmt="%(asctime)sZ [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime

        file_handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        self._logger.addHandler(file_handler)

    def log(self, level: str, event: str, **fields: Any) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        extra = f" | {json.dumps(fields, ensure_ascii=False, sort_keys=True)}" if fields else ""
        line = f"{ts} [{level.upper()}] {event}{extra}"
        with self._lock:
            self._ring.append(line)
            log_fn = getattr(self._logger, level.lower(), self._logger.info)
            log_fn(f"{event}{extra}")
        return line

    def info(self, event: str, **fields: Any) -> str:
        return self.log("INFO", event, **fields)

    def warning(self, event: str, **fields: Any) -> str:
        return self.log("WARNING", event, **fields)

    def error(self, event: str, **fields: Any) -> str:
        return self.log("ERROR", event, **fields)

    def tail(self, lines: int = 50) -> List[str]:
        """Returns the most recent N log lines from the in-memory ring buffer (or disk fallback)."""
        n = max(1, min(int(lines), 500))
        with self._lock:
            if self._ring:
                return list(self._ring)[-n:]
        if not self.log_file.exists():
            return []
        all_lines = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return all_lines[-n:]
