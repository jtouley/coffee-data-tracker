"""The admin's control plane: a Postgres database of who did what (docs/design/admin-registry.md §2).

It holds the audit trail, the owner's include/exclude decisions (as events), the job runs the admin started, the
decisions snapshots and the drafts built from them. Every table is append-only (a trigger refuses UPDATE, DELETE and
TRUNCATE), so the trail can't be rewritten through the admin. The lake is never here: the admin reads the lake and
only the pipeline's jobs write it.

Two ways to connect, one interface (`ControlPlane(connect)`):
- **hosted:** Cloud SQL for PostgreSQL through the Cloud SQL Python Connector with IAM database authentication: the
  service account is the database user, so there is no password anywhere (ADMIN_DB_INSTANCE, ADMIN_DB_USER,
  ADMIN_DB_NAME);
- **a laptop and the tests:** a local Postgres URL (ADMIN_DATABASE_URL), refused when hosted.

`migrate()` applies admin/migrations/*.sql in order, each once, under an advisory lock, so every instance can run
it at start. It creates the `control` schema it owns; the owner grants the IAM user CREATE on the database once
(docs/admin/README.md).
"""

from __future__ import annotations

import contextlib
import json
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from coffeedata import decisions

MIGRATIONS = Path(__file__).parent / "migrations"
IDLE_S = 120  # a pooled connection idle longer than this is dropped, not reused
LOCK = 0x61646D696E  # pg_advisory_xact_lock key for migrations ("admin")
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class ControlPlaneError(RuntimeError):
    """The control plane couldn't be read or written. Every write fails closed on it (503): nothing is started,
    decided or published without its audit row."""


class Conflict(Exception):
    """A write based on a stale view (another tab changed the subject first): 409, nothing written."""


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


def url_connect(url: str) -> Callable[[], Any]:
    """A local Postgres, from postgresql://user[:password]@host[:port]/db (a laptop, the tests)."""
    import pg8000.dbapi

    u = urlsplit(url)
    if u.scheme not in ("postgresql", "postgres") or not u.hostname or not u.path.strip("/"):
        raise ValueError("ADMIN_DATABASE_URL must look like postgresql://user@host:port/db")

    def connect():
        return pg8000.dbapi.connect(user=unquote(u.username or ""), password=unquote(u.password or "") or None,
                                    host=u.hostname, port=u.port or 5432, database=u.path.strip("/"),
                                    timeout=10)  # fmt: skip

    return connect


def cloudsql_connect(instance: str, user: str, db: str) -> Callable[[], Any]:
    """Cloud SQL with IAM database auth: the Connector fetches a short-lived certificate and an OAuth token for the
    service account (no password), over the instance's public IP with TLS (no authorized networks). The Connector is
    made on the first connection, not at start: it reads the service account's credentials, and a failure there is a
    refused write (503), never a service that doesn't start."""
    if not re.fullmatch(r"[a-z][a-z0-9-]+:[a-z]+-[a-z]+\d:[a-z][a-z0-9-]+", instance) or not user:
        raise ValueError("ADMIN_DB_INSTANCE is project:region:instance, and ADMIN_DB_USER the IAM user")
    made: list = []
    lock = threading.Lock()

    def connect():
        from google.cloud.sql.connector import Connector, IPTypes

        with lock:
            if not made:
                made.append(Connector(ip_type=IPTypes.PUBLIC, enable_iam_auth=True, refresh_strategy="lazy",
                                      timeout=15))  # fmt: skip
        return made[0].connect(instance, "pg8000", user=user, db=db)

    return connect


class ControlPlane:
    """The control plane over a connection factory. A small pool (`size` connections); a connection that errors is
    dropped. Every method is synchronous (the app calls them in a thread)."""

    def __init__(self, connect: Callable[[], Any], size: int = 4):
        self._connect = connect
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=size)
        self._migrated = False
        self._lock = threading.Lock()

    # --- connections and transactions ----------------------------------------------------------------------------

    def _take(self):
        """A pooled connection used in the last IDLE_S seconds, else a new one. An older one may have been closed by
        the server or the connector while the instance idled, so it is dropped rather than tried."""
        while True:
            try:
                con, used = self._pool.get_nowait()
            except queue.Empty:
                break
            if time.monotonic() - used < IDLE_S:
                return con
            with contextlib.suppress(Exception):
                con.close()
        try:
            return self._connect()
        except Exception as e:  # noqa: BLE001 - the driver's and the connector's errors vary
            raise ControlPlaneError(f"can't connect: {type(e).__name__}") from e

    @contextlib.contextmanager
    def tx(self) -> Iterator[Any]:
        """One transaction: committed if the block finishes, rolled back otherwise. A database or connection error is
        raised as ControlPlaneError (and the connection dropped); anything else the block raises (a Conflict, a
        refusal, a Cloud Run error) passes through as it is, after the rollback."""
        con = self._take()
        ok = False
        try:
            cur = con.cursor()
            yield cur
            con.commit()
            ok = True
        except BaseException as e:
            with contextlib.suppress(Exception):
                con.rollback()
            if isinstance(e, ControlPlaneError) or _from_database(e):
                raise ControlPlaneError(
                    f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
                ) from e
            ok = True  # the connection is fine: the block refused, not the database
            raise
        finally:
            if ok:
                try:
                    self._pool.put_nowait((con, time.monotonic()))
                except queue.Full:
                    con.close()
            else:
                with contextlib.suppress(Exception):
                    con.close()

    def migrate(self) -> list[str]:
        """Apply every migration not yet applied, in order, in one transaction under an advisory lock. Returns the
        ones applied now."""
        files = sorted(MIGRATIONS.glob("*.sql"))
        with self.tx() as cur:
            cur.execute("select pg_advisory_xact_lock(%s)", (LOCK,))
            cur.execute("create schema if not exists control")
            cur.execute("create table if not exists control.schema_migrations (name text primary key, "
                        "applied_at timestamptz not null default now())")  # fmt: skip
            cur.execute("select name from control.schema_migrations")
            done = {r[0] for r in cur.fetchall()}
            applied = []
            for f in files:
                if f.name in done:
                    continue
                cur.execute(f.read_text())
                cur.execute("insert into control.schema_migrations (name) values (%s)", (f.name,))
                applied.append(f.name)
        self._migrated = True
        return applied

    def ping(self) -> None:
        with self.tx() as cur:
            cur.execute("select 1")

    # --- writes ----------------------------------------------------------------------------------------------------

    @staticmethod
    def claim(cur, key: str) -> dict | None:
        """Claim an idempotency key in this transaction: None if it is new (go ahead), else the first answer."""
        if not key or len(key) > 200:
            raise ValueError("a write needs its idempotency key")
        cur.execute("select response from control.idempotency where key = %s", (key,))
        row = cur.fetchone()
        if row:
            return _json(row[0])
        return None

    def answered(self, key: str) -> dict | None:
        """The first answer to this idempotency key, if a write already used it (a double submit, a retry)."""
        with self.tx() as cur:
            return self.claim(cur, key)

    @staticmethod
    def remember(cur, key: str, response: dict) -> None:
        cur.execute("insert into control.idempotency (key, response) values (%s, %s::jsonb)",
                    (key, json.dumps(response, default=str)))  # fmt: skip

    @staticmethod
    def audit(cur, actor: str, action: str, subject: str | None, outcome: str, detail: dict | None = None,
              client: str = "ui") -> None:  # fmt: skip
        cur.execute(
            "insert into control.audit (actor, client, action, subject, outcome, detail) "
            "values (%s, %s, %s, %s, %s, %s::jsonb)",
            (actor, client, action, subject, outcome, json.dumps(detail or {}, default=str)),
        )

    def audit_now(
        self, actor: str, action: str, subject: str | None, outcome: str, detail: dict | None = None
    ) -> None:
        with self.tx() as cur:
            self.audit(cur, actor, action, subject, outcome, detail)

    def decide(self, actor: str, key: str, action: str, kind: str, reason: str, items: list[dict],
               seen: dict[str, str | None]) -> dict:  # fmt: skip
        """Approve `action` (include or exclude) for each item's subject (its coffee_id or input_hash, by `kind`),
        as one group when there are several. `seen` maps each subject to the decision_id the page showed as its
        latest event (None: none): a subject decided since makes the whole write a Conflict. Idempotent on `key`."""
        if action not in decisions.ACTIONS or kind not in decisions.SUBJECTS:
            raise ValueError("bad decision")
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 500:
            raise ValueError("give a reason (3 to 500 characters)")
        if not items or len(items) > 500:
            raise ValueError("decide 1 to 500 products at a time")
        with self.tx() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (key,))
            if (first := self.claim(cur, key)) is not None:
                return first
            subjects = sorted({str(it[kind]) for it in items if it.get(kind)})
            if not subjects:
                raise ValueError(f"the products have no {kind}")
            cur.execute("select pg_advisory_xact_lock(%s)", (LOCK + 1,))
            now = self._latest(cur, kind, subjects)
            stale = [s for s in subjects if (now.get(s) or {}).get("decision_id") != seen.get(s)]
            if stale:
                raise Conflict(
                    f"{len(stale)} of these changed since the page was loaded: reload and decide again"
                )
            group = new_id("g") if len(subjects) > 1 else None
            ids = []
            by_subject = {str(it[kind]): it for it in items if it.get(kind)}
            for s in subjects:
                it, did = by_subject[s], new_id("d")
                cur.execute(
                    "insert into control.decisions (decision_id, seq, event, subject_kind, subject, action, reason, "
                    "actor, group_id, coffee_id, input_hash, roaster_id, title) "
                    "values (%s, 1, 'approved', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (did, kind, s, action, reason, actor, group, it.get("coffee_id"), it.get("input_hash"),
                     it.get("roaster_id"), (it.get("title") or "")[:300]),
                )  # fmt: skip
                ids.append(did)
            out = {"ok": True, "decisions": ids, "group": group, "count": len(ids)}
            self.audit(cur, actor, "decide", group or f"{kind}:{subjects[0]}", "ok",
                       {"action": action, "kind": kind, "subjects": subjects[:50], "count": len(subjects),
                        "reason": reason, "decisions": ids})  # fmt: skip
            self.remember(cur, key, out)
            return out

    def undo(self, actor: str, key: str, target: str, reason: str) -> dict:
        """Revert a decision, or every decision of a group (`target` is a decision_id or a group_id): each subject
        goes back to the classifier. Only a decision that is still the latest event for its subject is reverted;
        any other is already superseded. Idempotent on `key`."""
        reason = (reason or "").strip() or "undo"
        if not ID.fullmatch(target):
            raise ValueError("bad decision id")
        with self.tx() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (key,))
            if (first := self.claim(cur, key)) is not None:
                return first
            cur.execute("select pg_advisory_xact_lock(%s)", (LOCK + 1,))
            cur.execute(
                "select decision_id, subject_kind, subject, action, max(seq) from control.decisions "
                "where (decision_id = %s or group_id = %s) group by 1, 2, 3, 4",
                (target, target),
            )
            rows = cur.fetchall()
            if not rows:
                raise ValueError("no such decision")
            reverted = []
            for did, kind, subject, _action, seq in rows:
                latest = self._latest(cur, kind, [subject]).get(subject)
                if not latest or latest["decision_id"] != did or latest["event"] != "approved":
                    continue
                cur.execute(
                    "insert into control.decisions (decision_id, seq, event, subject_kind, subject, action, reason, "
                    "actor, group_id, coffee_id, input_hash, roaster_id, title) "
                    "select decision_id, %s, 'reverted', subject_kind, subject, action, %s, %s, group_id, coffee_id, "
                    "input_hash, roaster_id, title from control.decisions where decision_id = %s and seq = 1",
                    (seq + 1, reason[:500] if len(reason) >= 3 else "undo", actor, did),
                )  # fmt: skip
                reverted.append(did)
            out = {"ok": True, "reverted": reverted, "count": len(reverted)}
            self.audit(cur, actor, "undo", target, "ok", {"reverted": reverted, "reason": reason})
            self.remember(cur, key, out)
            return out

    def record_run(self, cur, run: dict) -> None:
        cur.execute(
            "insert into control.job_runs (run_id, action, job, args, mode, week, test_id, roaster_id, execution, "
            "actor, override_reason) values (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)",
            (run["run_id"], run["action"], run["job"], json.dumps(run.get("args")), run.get("mode"),
             run.get("week"), run.get("test_id"), run.get("roaster_id"), run.get("execution"), run["actor"],
             run.get("override_reason")),
        )  # fmt: skip

    def run_status(
        self, run_id: str, status: str, duration_s: float | None = None, message: str | None = None
    ) -> None:
        """Record a run's outcome the first time the admin sees it (never changed after): the page then shows it
        without asking Cloud Run again."""
        with self.tx() as cur:
            cur.execute(
                "insert into control.job_run_status (run_id, status, duration_s, message) values (%s, %s, %s, %s) "
                "on conflict (run_id) do nothing",
                (run_id, status, duration_s, (message or "")[:500] or None),
            )

    def create_snapshot(self, cur, snapshot_id: str, sha256: str, n: int, actor: str) -> None:
        cur.execute("insert into control.snapshots (snapshot_id, sha256, decisions, actor) values (%s, %s, %s, %s)",
                    (snapshot_id, sha256, n, actor))  # fmt: skip

    def create_draft(self, cur, draft_id: str, snapshot_id: str, baseline: str | None, run_id: str | None,
                     actor: str) -> None:  # fmt: skip
        cur.execute("insert into control.drafts (draft_id, snapshot_id, baseline, run_id, actor) "
                    "values (%s, %s, %s, %s, %s)", (draft_id, snapshot_id, baseline, run_id, actor))  # fmt: skip

    def draft_event(self, cur, draft_id: str, event: str, actor: str, detail: dict | None = None) -> None:
        cur.execute("insert into control.draft_events (draft_id, event, actor, detail) values (%s, %s, %s, %s::jsonb)",
                    (draft_id, event, actor, json.dumps(detail or {}, default=str)))  # fmt: skip

    # --- reads -----------------------------------------------------------------------------------------------------

    @staticmethod
    def _latest(cur, kind: str, subjects: list[str]) -> dict[str, dict]:
        if not subjects:
            return {}
        cur.execute(
            "select distinct on (subject) subject, decision_id, event, action, group_id from control.decisions "
            "where subject_kind = %s and subject = any(%s) order by subject, id desc",
            (kind, subjects),
        )
        return {
            r[0]: {"decision_id": r[1], "event": r[2], "action": r[3], "group_id": r[4]}
            for r in cur.fetchall()
        }

    def latest(self, coffee_ids: list[str], input_hashes: list[str]) -> dict[str, dict[str, dict]]:
        """The latest decision event for each coffee and each version: {"coffee_id": {...}, "input_hash": {...}}."""
        with self.tx() as cur:
            return {"coffee_id": self._latest(cur, "coffee_id", coffee_ids),
                    "input_hash": self._latest(cur, "input_hash", input_hashes)}  # fmt: skip

    def effective(self) -> list[dict]:
        """One decision per subject: the latest event, when it is an approval (a reverted subject has none)."""
        with self.tx() as cur:
            cur.execute(
                "select * from (select distinct on (subject_kind, subject) decision_id, event, subject_kind, subject, "
                "action, reason, actor, to_char(at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), coffee_id, "
                "input_hash, roaster_id, title from control.decisions order by subject_kind, subject, id desc) d "
                "where event = 'approved' order by subject_kind, subject"
            )
            cols = ("decision_id", "event", "subject_kind", "subject", "action", "reason", "actor", "decided_at",
                    "coffee_id", "input_hash", "roaster_id", "title")  # fmt: skip
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def audit_log(
        self, limit: int = 100, offset: int = 0, action: str | None = None
    ) -> tuple[list[dict], int]:
        with self.tx() as cur:
            where, params = ("where action = %s", [action]) if action else ("", [])
            cur.execute(f"select count(*) from control.audit {where}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"select id, at, actor, client, action, subject, outcome, detail from control.audit {where} "
                "order by id desc limit %s offset %s",
                [*params, limit, offset],
            )
            cols = ("id", "at", "actor", "client", "action", "subject", "outcome", "detail")
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
            for r in rows:
                r["detail"] = _json(r["detail"])
            return rows, total

    def runs(self, limit: int = 30) -> list[dict]:
        with self.tx() as cur:
            cur.execute(
                "select r.run_id, r.action, r.job, r.args, r.mode, r.week, r.test_id, r.roaster_id, r.execution, "
                "r.actor, r.override_reason, r.started_at, s.status, s.duration_s, s.message from control.job_runs r "
                "left join control.job_run_status s using (run_id) order by r.started_at desc limit %s",
                (limit,),
            )
            cols = ("run_id", "action", "job", "args", "mode", "week", "test_id", "roaster_id", "execution", "actor",
                    "override_reason", "started_at", "final", "duration_s", "message")  # fmt: skip
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
            for r in rows:
                r["args"] = _json(r["args"])
            return rows

    def run(self, run_id: str) -> dict | None:
        """One run the admin started, or None."""
        with self.tx() as cur:
            cur.execute("select run_id, job, execution from control.job_runs where run_id = %s", (run_id,))
            r = cur.fetchone()
            return dict(zip(("run_id", "job", "execution"), r, strict=True)) if r else None

    def recent_test(self, roaster_id: str, hours: float = 24) -> dict | None:
        """The latest test crawl of this roaster started in the last `hours`, if any."""
        with self.tx() as cur:
            cur.execute(
                "select r.run_id, r.started_at, s.status, r.execution, r.test_id from control.job_runs r "
                "left join control.job_run_status s using (run_id) "
                "where r.roaster_id = %s and r.mode = 'test' and r.started_at > now() - make_interval(secs => %s) "
                "order by r.started_at desc limit 1",
                (roaster_id, hours * 3600),
            )
            r = cur.fetchone()
            return (
                dict(zip(("run_id", "started_at", "status", "execution", "test_id"), r, strict=True))
                if r
                else None
            )

    def tests_since(self, hours: float) -> list[dict]:
        """Every test crawl started in the last `hours`: one roaster's (roaster_id) or a whole connector's (none)."""
        with self.tx() as cur:
            cur.execute(
                "select run_id, test_id, roaster_id, args from control.job_runs where mode = 'test' "
                "and started_at > now() - make_interval(secs => %s) order by started_at desc",
                (hours * 3600,),
            )
            rows = [
                dict(zip(("run_id", "test_id", "roaster_id", "args"), r, strict=True)) for r in cur.fetchall()
            ]
        for r in rows:
            r["args"] = _json(r["args"]) or []
            r["connector"] = (
                r["args"][r["args"].index("--connector") + 1] if "--connector" in r["args"] else None
            )
        return rows

    def step9_total(self, published: set[str]) -> tuple[int, dict | None]:
        """CLAUDE.md step 9's running total: the public coffees changed by the drafts published (their build in
        `published`, from serving_log) since the last recorded full evaluation, and that evaluation."""
        with self.tx() as cur:
            cur.execute("select at, path, actor from control.evaluations order by id desc limit 1")
            ev = cur.fetchone()
            last = dict(zip(("at", "path", "actor"), ev, strict=True)) if ev else None
            cur.execute(
                "select detail from control.draft_events where event = 'publish_started' and (%s::timestamptz is null "
                "or at > %s::timestamptz)",
                (last and last["at"], last and last["at"]),
            )
            total = 0
            for (detail,) in cur.fetchall():
                d = _json(detail) or {}
                if d.get("build") in published:
                    total += int(d.get("changed") or 0)
        return total, last

    def record_evaluation(self, actor: str, key: str, path: str, reason: str) -> dict:
        """A full evaluation of the classifier's output, done: the running total starts again from here."""
        with self.tx() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (key,))
            if (first := self.claim(cur, key)) is not None:
                return first
            cur.execute("insert into control.evaluations (actor, path, reason) values (%s, %s, %s)",
                        (actor, path, reason))  # fmt: skip
            self.audit(cur, actor, "evaluation", path, "ok", {"reason": reason})
            out = {"ok": True, "evaluation": path}
            self.remember(cur, key, out)
            return out

    def drafts(self, limit: int = 20) -> list[dict]:
        with self.tx() as cur:
            cur.execute(
                "select d.draft_id, d.snapshot_id, s.sha256, s.decisions, d.baseline, d.run_id, d.actor, d.created_at, "
                "(select event from control.draft_events e where e.draft_id = d.draft_id order by id desc limit 1) "
                "from control.drafts d join control.snapshots s using (snapshot_id) order by d.created_at desc limit %s",
                (limit,),
            )
            cols = ("draft_id", "snapshot_id", "sha256", "decisions", "baseline", "run_id", "actor", "created_at",
                    "last_event")  # fmt: skip
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _from_database(e: BaseException) -> bool:
    """An error from the database, the driver or the connection (as opposed to the block's own refusal)."""
    return isinstance(e, OSError | TimeoutError) or type(e).__module__.split(".")[0] in (
        "pg8000", "google", "aiohttp", "ssl", "scramp", "asyncio")  # fmt: skip


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v
