"""In-memory sliding-window failure counter for login.

Fine for a single api process. On AWS this moves to a shared store (DynamoDB/ElastiCache or a DB table).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class FailureLimiter:
    def __init__(self, window_seconds: int) -> None:
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def count(self, key: str) -> int:
        with self._lock:
            return len(self._prune(key, time.monotonic()))

    def blocked(self, key: str, limit: int) -> bool:
        return self.count(key) >= limit

    def record(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
