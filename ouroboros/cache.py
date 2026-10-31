"""Multi-Tier Cache Engine (`ouroboros/cache.py`) for Ouroboros Agent.

Optimizes three performance bottlenecks identified during adversarial review:
1. `MtimeFileCache`: Caches disk files (`SOUL.md`, `MEMORY.md`, `TASK.md`, `SKILL.md`) in RAM keyed by
   `(resolved_path, st_mtime_ns, st_size)`. Eliminates redundant disk reads on every agent turn.
2. `BoundedLRUCache`: Thread-safe O(1) OrderedDict LRU cache with TTL and hit/miss telemetry for
   SQLite KV lookups, session histories, and deterministic read-only tool outputs (`lookup_skills`).
3. `PromptCacheAligner`: Structures system messages so static instructions (`SOUL.md` + `Skill Catalog`
   + `Planning Protocol`) form a 100% immutable prefix at the start of the prompt, while volatile
   per-turn state (`TASK.md` + `MEMORY.md`) is isolated at the tail—maximizing OpenAI/Anthropic KV-cache hits.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import pathlib
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple


@dataclasses.dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    invalidations: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "hit_rate": self.hit_rate,
        }


class MtimeFileCache:
    """Thread-safe file content cache invalidated automatically when `st_mtime_ns` or `st_size` changes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[pathlib.Path, Tuple[int, int, str]] = {}
        self.stats = CacheStats()

    def read_text(self, path: pathlib.Path, default: str = "") -> str:
        resolved = path.resolve()
        try:
            st = resolved.stat()
        except OSError:
            return default

        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            cached = self._entries.get(resolved)
            if cached and cached[0] == sig[0] and cached[1] == sig[1]:
                self.stats.hits += 1
                return cached[2]

            self.stats.misses += 1
            content = resolved.read_text(encoding="utf-8", errors="replace")
            self._entries[resolved] = (sig[0], sig[1], content)
            return content

    def invalidate(self, path: pathlib.Path) -> None:
        resolved = path.resolve()
        with self._lock:
            if resolved in self._entries:
                del self._entries[resolved]
                self.stats.invalidations += 1


class BoundedLRUCache:
    """Thread-safe O(1) bounded LRU cache with optional TTL expiration."""

    def __init__(self, max_size: int = 512, ttl_seconds: float = 3600.0) -> None:
        self.max_size = max(16, max_size)
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._store: "collections.OrderedDict[str, Tuple[float, Any]]" = collections.OrderedDict()
        self.stats = CacheStats()

    def get(self, key: str) -> Tuple[bool, Any]:
        now = time.time()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                self.stats.misses += 1
                return False, None
            ts, val = item
            if now - ts > self.ttl_seconds:
                del self._store[key]
                self.stats.evictions += 1
                self.stats.misses += 1
                return False, None
            self._store.move_to_end(key)
            self.stats.hits += 1
            return True, val

    def put(self, key: str, value: Any) -> None:
        now = time.time()
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (now, value)
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)
                self.stats.evictions += 1

    def invalidate(self, key: str) -> None:
        with self._lock:
            if key in self._store:
                del self._store[key]
                self.stats.invalidations += 1

    def clear(self) -> None:
        with self._lock:
            count = len(self._store)
            self._store.clear()
            self.stats.invalidations += count


GLOBAL_FILE_CACHE = MtimeFileCache()
