"""A fake Cloud Run Jobs API for the admin's tests (admin/runjobs.py's `Jobs`): executions held in memory, finished
by the test (`finish`), refused on demand (`refuse`), slow on demand (`delay_s`). Every call is recorded."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from admin.runjobs import Execution, JobsError

PARENT = "projects/coffee-test-1/locations/us-central1"


class FakeJobs:
    def __init__(self) -> None:
        self.ex: dict[str, Execution] = {}
        self.calls: list[tuple] = []
        self.refuse: set[str] = set()  # jobs whose start Cloud Run refuses
        self.down = False  # every call fails, as when Cloud Run doesn't answer
        self.delay_s = 0.0
        self._n = 0
        self._lock = threading.Lock()

    def _check(self) -> None:
        if self.down:
            raise JobsError("Cloud Run didn't answer: ConnectTimeout")

    def run(self, job: str, args: list[str] | None) -> Execution:
        self._check()
        if self.delay_s:
            time.sleep(self.delay_s)
        with self._lock:
            self.calls.append(("run", job, args))
            if job in self.refuse:
                raise JobsError("Cloud Run refused (403): Permission 'run.jobs.runWithOverrides' denied")
            self._n += 1
            short = f"{job}-{self._n:05d}"
            name = f"{PARENT}/jobs/{job}/executions/{short}"
            now = datetime.now(UTC)
            e = Execution(name, job, "running", created=now, started=now,
                          log_uri=f"https://console.cloud.google.com/run/jobs/executions/details/us-central1/{short}/logs?project=coffee-test-1")  # fmt: skip
            self.ex[name] = e
            return e

    def executions(self, job: str, limit: int = 10) -> list[Execution]:
        self._check()
        self.calls.append(("list", job))
        return [e for e in reversed(self.ex.values()) if e.job == job][:limit]

    def execution(self, name: str) -> Execution:
        self._check()
        if name not in self.ex:
            raise ValueError("bad execution name")
        return self.ex[name]

    def cancel(self, name: str) -> None:
        self._check()
        self.calls.append(("cancel", name))
        e = self.ex[name]
        e.status, e.completed = "cancelled", datetime.now(UTC)

    def finish(self, job: str | None = None, status: str = "succeeded", message: str | None = None) -> None:
        for e in self.ex.values():
            if e.running and (job is None or e.job == job):
                e.status, e.completed = status, datetime.now(UTC)
                e.message = message if status == "failed" else None
