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
        self.on_finish = None  # Pipeline: what the job writes when it ends
        self.args: dict[str, list[str] | None] = {}
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
            self.args[name] = args
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
        for e in list(self.ex.values()):
            if e.running and (job is None or e.job == job):
                if self.on_finish:
                    self.on_finish(e, self.args.get(e.name), status)
                e.status, e.completed = status, datetime.now(UTC)
                e.message = message if status == "failed" else None


class Pipeline:
    """What the real jobs would write when a fake execution finishes, into a copy of the test lake (never the admin):
    a test crawl's job_manifests rows (so the store "answered"), a draft build's builds rows, release files and
    release_changes (cli._build's records), and a publish or rollback's serving_log row and release pointer
    (quality.serve). Attach with `fake.on_finish = Pipeline(...)`."""

    def __init__(self, lake_root, snapshots_root, week: str):
        self.lake_root, self.snapshots_root, self.week = lake_root, snapshots_root, week

    def __call__(self, e: Execution, args: list[str] | None, status: str) -> None:
        import json
        import re
        import shutil

        import pyarrow as pa
        import pyarrow.parquet as pq

        from coffeedata import control, decisions
        from coffeedata.lake import SCHEMAS, Lake

        lake = Lake(str(self.lake_root))
        a = args or []
        script = a[0] if len(a) == 1 else ""
        if "--test" in a:
            test = a[a.index("--test") + 1]
            conn = a[a.index("--connector") + 1]
            job = control.start(lake, "crawl", self.week, conn, 1, detail={"test": test, "arm": "test"})
            control.finish(lake, job, "done" if status == "succeeded" else "failed", ok=int(status == "succeeded"),
                           errors=int(status != "succeeded"), detail={"test": test, "arm": "test"})  # fmt: skip
        elif m := re.search(r"--draft (\S+) --snapshot (\S+) --snapshot-sha256 (\S+)", script):
            if status != "succeeded":
                return
            draft, sid = m.group(1), m.group(2)
            doc = json.loads((self.snapshots_root / decisions.key(sid)).read_text())
            served = pq.read_table(self.lake_root / "marts" / "release.parquet").to_pylist()[0]
            base = served["build_id"]
            build = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{draft[-6:]}"
            src = next(self.lake_root.glob(f"releases/*/build={base}"))
            dst = self.lake_root / "releases" / f"run_date={self.week}" / f"build={build}"
            shutil.copytree(src, dst)
            (dst / "ops" / "release_changes.parquet").unlink(missing_ok=True)
            passed = True  # a failed draft build: finish its execution as failed (nothing recorded here)
            now = datetime.now(UTC)
            row = {"build_id": build, "built_at": now, "run_date": datetime.fromisoformat(self.week).date()}
            lake.write("builds", f"builds/run_date={self.week}/build={build}.parquet", [
                row | {"kind": "build", "unique_id": "build", "status": "pass" if passed else "fail"},
                row | {"kind": "node", "unique_id": "test.decisions_applied", "resource_type": "test",
                       "status": "pass" if passed else "fail", "failures": 0 if passed else 1},
                row | {"kind": "draft", "unique_id": draft},
                row | {"kind": "snapshot", "unique_id": sid, "checksum": decisions.digest(doc),
                       "payload": json.dumps(doc, sort_keys=True)},
            ])  # fmt: skip
            changes = [{"build_id": build, "table_name": "public_coffee", "item": d["subject"],
                        "change": "removed" if d["action"] == "exclude" else "added",
                        "reason": f"decision:{d['action']}"} for d in doc["decisions"]]  # fmt: skip
            lake.write_ops("release_changes", self.week, build, pa.Table.from_pylist(changes, SCHEMAS["release_changes"]))
        elif a[:1] == ["publish-release"] and status == "succeeded":
            build, action = a[a.index("--build") + 1], a[a.index("--action") + 1]
            run_date = next(self.lake_root.glob(f"releases/*/build={build}")).parent.name.split("=")[1]
            now = datetime.now(UTC)
            pq.write_table(pa.Table.from_pylist([{"build_id": build, "run_date": datetime.fromisoformat(run_date).date(),
                                                  "served_at": now, "reason": action}]),
                           self.lake_root / "marts" / "release.parquet")  # fmt: skip
            lake.write("serving_log", f"serving_log/serve_id={now:%Y%m%dT%H%M%S%f}.parquet", [{
                "served_at": now, "action": action, "build_id": build,
                "run_date": datetime.fromisoformat(run_date).date(), "reason": action, "actor": "publish job"}])  # fmt: skip
