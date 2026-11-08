"""Counting requests per key within a window, in memory (app/accounts.py's limits and app/mail.py's daily cap).

In memory means per process: a restart, which includes every cold start of the scale-to-zero service, starts the
counts again. They slow down a burst from one place within one instance's life; they don't stop a slow or
spread-out attempt (docs/app/RUNNING.md, "Email", says so)."""

from __future__ import annotations

import threading
from collections import defaultdict, deque

SWEEP_AT = 10_000


class Windows:
    """Counts per key within a window of its own length, in memory (a restart clears it). `take` counts one
    request against every key unless any is already at its limit, under one lock."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def take(self, limits: dict[str, tuple[int, float]], now: float) -> bool:
        with self.lock:
            if len(self.hits) > SWEEP_AT:
                for key in [k for k, q in self.hits.items() if not q or q[-1] <= now - 86400]:
                    del self.hits[key]
            for key, (_, window) in limits.items():
                q = self.hits[key]
                while q and q[0] <= now - window:
                    q.popleft()
            if any(len(self.hits[k]) >= n for k, (n, _) in limits.items()):
                return False
            for k in limits:
                self.hits[k].append(now)
            return True

    def refund(self, key: str, at: float) -> None:
        """Take back one counted request (it didn't happen after all)."""
        with self.lock:
            q = self.hits.get(key)
            if q and at in q:
                q.remove(at)
