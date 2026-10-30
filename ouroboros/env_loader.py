"""Zero-dependency, hot-reloadable `.env` file loader and environment normalizer.

Solves two critical operational failures on dedicated servers:
1. Running `python3 main.py` or systemd without manually sourcing `.env`
2. Modifying `.env` (e.g., adding `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, or switching `OUROBOROS_MODEL`)
   while the daemon is already running — changes are detected via `st_mtime_ns` and applied live without restart.
"""

from __future__ import annotations

import os
import pathlib
from typing import Dict, List, Optional, Tuple


TELEGRAM_TOKEN_KEYS = (
    "OUROBOROS_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_API",
    "TELEGRAM_API_KEY",
    "TELEGRAM_TOKEN",
    "BOT_TOKEN",
)

TELEGRAM_CHAT_ID_KEYS = (
    "OUROBOROS_TELEGRAM_CHAT_ID",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "ALLOWED_CHAT_ID",
    "CHAT_ID",
)


class LiveEnvLoader:
    """Watches `.env` files and synchronizes values into `os.environ` without restarting the process."""

    def __init__(self, search_paths: Optional[List[pathlib.Path]] = None) -> None:
        if search_paths:
            self.search_paths = [p.resolve() for p in search_paths]
        else:
            candidates = []
            if os.environ.get("OUROBOROS_ENV_FILE"):
                candidates.append(pathlib.Path(os.environ["OUROBOROS_ENV_FILE"]))
            repo_root = pathlib.Path(__file__).resolve().parent.parent
            candidates.append(repo_root / ".env")
            candidates.append(pathlib.Path.cwd() / ".env")
            candidates.append(pathlib.Path("/opt/ouroboros-agent/.env"))
            # Deduplicate while preserving order
            seen = set()
            self.search_paths = []
            for c in candidates:
                rc = c.resolve()
                if rc not in seen:
                    seen.add(rc)
                    self.search_paths.append(rc)

        self._last_mtimes: Dict[pathlib.Path, int] = {}
        self.last_loaded_files: List[str] = []
        self.reload_if_changed(force=True)

    @staticmethod
    def parse_dotenv_content(raw_text: str) -> Dict[str, str]:
        """Parses standard `.env` syntax (`KEY=VAL`, `export KEY="VAL"`, `# comments`)."""
        parsed: Dict[str, str] = {}
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if not key:
                continue
            # Strip matching quotes or inline unquoted comments
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            elif " #" in val:
                val = val.split(" #", 1)[0].strip()
            parsed[key] = val
        return parsed

    def reload_if_changed(self, force: bool = False) -> Tuple[bool, List[str]]:
        """Checks `.env` mtimes and updates `os.environ` in-place if any file changed."""
        changed = False
        updated_keys: List[str] = []
        loaded_files: List[str] = []

        for path in self.search_paths:
            if not path.is_file():
                continue
            loaded_files.append(str(path))
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                continue
            if not force and self._last_mtimes.get(path) == mtime_ns:
                continue

            self._last_mtimes[path] = mtime_ns
            changed = True
            entries = self.parse_dotenv_content(path.read_text(encoding="utf-8", errors="replace"))
            for k, v in entries.items():
                if os.environ.get(k) != v:
                    os.environ[k] = v
                    updated_keys.append(k)

        self.last_loaded_files = loaded_files
        return changed, updated_keys


def resolve_telegram_credentials() -> Tuple[str, List[str]]:
    """Resolves Telegram Bot Token and allowed Chat IDs across all common `.env` key names."""
    token = ""
    for k in TELEGRAM_TOKEN_KEYS:
        val = os.environ.get(k, "").strip()
        if val and not val.startswith("replace-with") and not val.startswith("YOUR_"):
            token = val
            break

    chat_ids: List[str] = []
    for k in TELEGRAM_CHAT_ID_KEYS:
        raw = os.environ.get(k, "").strip()
        if raw and not raw.startswith("replace-with") and not raw.startswith("YOUR_"):
            for part in raw.replace(";", ",").split(","):
                clean = part.strip()
                if clean and clean not in chat_ids:
                    chat_ids.append(clean)
    return token, chat_ids
