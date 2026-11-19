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
        """A key is only made once a request is counted, so refused requests (random names past a limit) add
        nothing (final review N7)."""
        with self.lock:
            if len(self.hits) > SWEEP_AT:
                for key in [k for k, q in self.hits.items() if not q or q[-1] <= now - 86400]:
                    del self.hits[key]
            for key, (_, window) in limits.items():
                q = self.hits.get(key)
                while q and q[0] <= now - window:
                    q.popleft()
            if any(len(self.hits.get(k) or ()) >= n for k, (n, _) in limits.items()):
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


def forwarded_client(forwarded_for: str, peer: str | None, hops: int) -> str:
    """The caller's address: behind `hops` proxies that each append to X-Forwarded-For, that many entries from the
    right (anything further left came from the client and can't be trusted); else the socket's peer. The one rule for
    the app (web.client_ip) and the usage collector (app/usage.py)."""
    if hops > 0:
        chain = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return peer or ""


class FixedWindows:
    """Counts per key in fixed windows (this minute, this UTC day), in memory: one integer per key and window, however
    many requests, unlike Windows' timestamp per hit. A burst across a window's edge can get up to twice the limit; for
    analytics events that is fine. Counts from past windows are swept once there are more than SWEEP_AT, at most once a
    minute. Memory is bounded by the callers' own caps (the usage collector's daily cap bounds its keys a day)."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, float, int], int] = {}
        self.lock = threading.Lock()
        self.swept = -1  # the minute of the last sweep: at most one a minute, never one per request

    def take(self, limits: dict[str, tuple[int, float]], now: float) -> bool:
        """Count one against every key unless any is at its limit (key -> (limit, window seconds))."""
        return self.blocked(limits, now) is None

    def blocked(self, limits: dict[str, tuple[int, float]], now: float) -> str | None:
        """The first key (in the dict's order) already at its limit, counting nothing; else None, having counted one
        against every key."""
        with self.lock:
            slots = [((k, window, int(now // window)), n) for k, (n, window) in limits.items()]
            if len(self.counts) > SWEEP_AT and int(now // 60) != self.swept:
                self.swept = int(now // 60)
                for slot in [s for s in self.counts if s[2] < int(now // s[1])]:
                    del self.counts[slot]
            for slot, n in slots:
                if self.counts.get(slot, 0) >= n:
                    return slot[0]
            for slot, _ in slots:
                self.counts[slot] = self.counts.get(slot, 0) + 1
            return None
