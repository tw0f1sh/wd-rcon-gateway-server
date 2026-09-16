from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self, global_limit: int, per_key_limit: int, window_seconds: float = 60.0) -> None:
        self.global_limit = max(0, int(global_limit))
        self.per_key_limit = max(0, int(per_key_limit))
        self.window = float(window_seconds)
        self._global: deque[float] = deque()
        self._per_key: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def allow(self, key_id: str) -> tuple[bool, str | None]:
        now = time.monotonic()
        with self._lock:
            self._trim(self._global, now)
            key_bucket = self._per_key[key_id]
            self._trim(key_bucket, now)
            if self.global_limit and len(self._global) >= self.global_limit:
                return False, "global"
            if self.per_key_limit and len(key_bucket) >= self.per_key_limit:
                return False, "key"
            self._global.append(now)
            key_bucket.append(now)
            return True, None
