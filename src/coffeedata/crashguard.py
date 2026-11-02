"""The poison-pill guard: a roaster that kills its crawl (an OOM kill, a segfault, an uncaught error) must not kill
every resume of that crawl too.

Before a roaster's fetch starts, the crawl writes a marker for it, and removes the marker when the fetch returns:

    raw/connector=<name>/run_date=<week>/[slice=<k>-of-<n>/]_inflight/attempt=<attempt_id>/<roaster_id>.json

An attempt that ends normally (its job_manifests end row says done or deadline) removes its folder. So the folder of
an attempt without such an end row (only `running`, or `failed`) holds the roasters that were in flight when it
died. The next attempt of the same connector and week reads them before it starts:

- a roaster in flight in one dead attempt is a suspect: the attempt fetches its suspects one at a time (a "lane"
  beside the other workers), so if it dies again only one suspect is in flight with whatever else it was fetching;
- a roaster in flight in QUARANTINE_AFTER (2) dead attempts is quarantined for the week: the attempt writes it a
  fetches row with status `crashed` instead of fetching it, and every later pass of the week skips it, as it skips
  an `ok` or `unavailable` one. Next week it is fetched as usual (the folders live in the week's partition, which
  the merge removes).

The markers live in the staging folder (the GCSFuse mount on Cloud Run), which survives the container. They are only
ever created, written once and deleted, which GCSFuse supports (it refuses chmod). The guard needs no lock: one
attempt of a connector runs at a time (Cloud Run starts a retry only after the attempt ended), and an arm's slice has
its own folder. RUNBOOK §7 says how to fetch a quarantined roaster again within the week.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

QUARANTINE_AFTER = 2  # dead attempts a roaster was in flight in before it is quarantined for the week
CRASHED = "crashed"  # its fetches status (lake.FETCH_STATUSES)


def root(part: Path) -> Path:
    """The in-flight markers of a connector's partition (connectors.partition, or an arm's slice of it)."""
    return part / "_inflight"


class Guard:
    """One attempt's in-flight markers. start and end run in the worker that fetches the roaster (ingest._attempt),
    so a marker is on disk before the fetch's first request and only while it runs. Each roaster has its own file:
    no lock. Best effort: a marker that can't be written or removed is logged, and never fails the fetch."""

    def __init__(self, root: Path, attempt_id: str):
        self.dir = root / f"attempt={attempt_id}"

    def _marker(self, roaster_id: str) -> Path:
        return self.dir / f"{roaster_id}.json"

    def start(self, roaster_id: str) -> None:
        at = datetime.now(UTC).replace(microsecond=0).isoformat()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._marker(roaster_id).write_text(json.dumps({"roaster_id": roaster_id, "started_at": at}))
        except OSError as e:
            log.warning("in-flight marker for %s not written: %s", roaster_id, e)

    def end(self, roaster_id: str) -> None:
        try:
            self._marker(roaster_id).unlink(missing_ok=True)
        except OSError as e:
            log.warning("in-flight marker for %s not removed: %s", roaster_id, e)

    def close(self) -> None:
        """The attempt ended normally (after its end row is written): nothing of it is suspect."""
        shutil.rmtree(self.dir, ignore_errors=True)


@dataclass
class Health:
    """What the earlier attempts of this connector's week left behind."""

    dead: list[str] = field(default_factory=list)  # attempt ids that died (no done or deadline end row)
    suspects: dict[str, list[str]] = field(
        default_factory=dict
    )  # roaster -> the dead attempts it was in flight in

    def quarantine(self, todo: set[str] | None = None) -> dict[str, str]:
        """roaster -> its fetch error, for every roaster in flight in QUARANTINE_AFTER or more dead attempts (and in
        `todo`, when given: one already done this week needs no row)."""
        return {
            rid: f"{CRASHED}: in flight when {len(ids)} attempts of this crawl died ({', '.join(ids)}); "
            "quarantined for this week (RUNBOOK §7)"
            for rid, ids in sorted(self.suspects.items())
            if len(ids) >= QUARANTINE_AFTER and (todo is None or rid in todo)
        }

    def summary(self, todo: set[str] | None = None) -> dict:
        return {"dead_attempts": self.dead, "suspects": sorted(self.suspects),
                "quarantined": sorted(self.quarantine(todo))}  # fmt: skip


def assess(root: Path, finished: set[str]) -> Health:
    """The dead attempts under `root` and their in-flight roasters. finished: the attempt ids with a done or
    deadline end row (control.rows); a leftover folder of one of those (killed between its end row and its cleanup)
    is removed, not counted."""
    health = Health()
    for d in sorted(root.glob("attempt=*")) if root.exists() else []:
        attempt = d.name.removeprefix("attempt=")
        if attempt in finished:
            shutil.rmtree(d, ignore_errors=True)
            continue
        health.dead.append(attempt)
        for m in sorted(d.glob("*.json")):
            health.suspects.setdefault(m.stem, []).append(attempt)
    return health
