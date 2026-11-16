"""Small in-process cache with an explicit expiry.

Deliberately not ``functools.lru_cache``: entries here wrap data that goes stale
(minted credentials, admin-editable configuration), so the staleness window has to
be visible at the call site and bounded in time rather than by size.
"""

import threading
import time
from typing import Generic, Hashable, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = ttl_seconds
        self._entries: dict[Hashable, tuple[float, T]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> T | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= time.monotonic():
                del self._entries[key]
                return None
            return value

    def put(self, key: Hashable, value: T) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + self.ttl_seconds, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
