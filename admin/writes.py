"""Every write the admin makes (docs/design/admin-registry.md §3, §5.2): start or cancel a pipeline job, decide or
undo an include/exclude, build a draft, Publish, Roll back, Discard.

Each write:
- is checked first (fields, the job guards, the Publish readiness), and refused with every reason;
- is recorded in the control plane in the same transaction as its effect, with who, what and the outcome, so a
  write the database can't record doesn't happen (fail closed: ControlPlaneError, 503);
- carries an idempotency key, so a double submit or a retry acts once and returns the first answer;
- never writes the lake: the pipeline's jobs do (the build, the publish job). The admin writes only its database
  and its snapshot bucket.
A structured log line (`admin_write`) goes with every write, which the owner's alert emails (src/terraform/admin.tf).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from coffeedata import decisions as snap

from . import jobs as registry
from . import queries
from .controlplane import ControlPlane, new_id
from .runjobs import Jobs, JobsError
from .store import Store

log = logging.getLogger("admin")
TYPE_OVER = 50  # Publish asks for the count of public rows that leave above this (design §3)
LIVE_CHECK_DAYS = 7
REASON = re.compile(r"[^\x00-\x1f\x7f]{3,300}")


def emit(**fields) -> None:
    log.info(json.dumps({"severity": "NOTICE", "event": "admin_write", **fields}, default=str))


def reason_of(raw: object, what: str = "a reason") -> str:
    r = str(raw or "").strip()
    if not REASON.fullmatch(r):
        raise ValueError(f"Give {what}: 3 to 300 characters, one line")
    return r


@dataclass
class Readiness:
    """Publish's four conditions and the live check (design §3), each with the reason it holds or doesn't."""

    checks: list[dict]
    removed: int | None
    needs_count: bool

    @property
    def ready(self) -> bool:
        return all(c["ok"] for c in self.checks)


class Writes:
    def __init__(self, control: ControlPlane, jobs: Jobs, snapshots: Store,
                 read: Callable[..., Awaitable[Any]], now: Callable[[], datetime] = lambda: datetime.now(UTC)):  # fmt: skip
        self.control, self.jobs, self.snapshots, self.read, self.now = control, jobs, snapshots, read, now
        self._migrated = False

    async def ready(self) -> None:
        """Migrate the control plane once (idempotent), before the first write or read of it."""
        if not self._migrated:
            await asyncio.to_thread(self.control.migrate)
            self._migrated = True

    # --- jobs ------------------------------------------------------------------------------------------------------

    async def plan(self, key: str, raw: dict) -> registry.Plan:
        """The steps an action would start, with the guards' refusals and the window it falls in: the confirm step
        shows exactly this, and `start` checks it again."""
        action = registry.ACTIONS.get(key)
        if action is None or action.page != "jobs":
            raise ValueError("no such job action")
        await self.ready()
        today = self.now().date()
        params = registry.checked(action, raw, today, lambda: new_id("t").lower()[:40])
        return await self._guarded(action, params)

    async def _guarded(self, action: registry.Action, params: dict) -> registry.Plan:
        steps = action.steps(params)
        plan = registry.Plan(action, params, steps)
        current_week = registry.week_of(self.now().date().isoformat())
        current = registry.WeekState(**await self.read(queries.week_state, current_week))
        target = current if params.get("week", current_week) == current_week else registry.WeekState(
            **await self.read(queries.week_state, params["week"]))  # fmt: skip
        running = await asyncio.to_thread(self._running, {s.job for s in steps})
        recent = None
        if action.key == "crawl-one" and params.get("mode") == "test":
            recent = await asyncio.to_thread(self.control.recent_test, params["roaster"], 24)
        return registry.guard(plan, self.now(), current, target, running, recent)

    def _running(self, names: set[str]) -> dict[str, str]:
        out = {}
        for job in sorted(names):
            for e in self.jobs.executions(job, 5):
                if e.running:
                    out[job] = e.short
        return out

    async def replay(self, key: str) -> dict | None:
        """A write's first answer when its key was used: checked before the guards, so a double submit returns what
        the first one did instead of being refused by the run it started."""
        await self.ready()
        return await asyncio.to_thread(self.control.answered, key)

    async def start(self, actor: str, key: str, action_key: str, raw: dict, override: str | None) -> dict:
        if (first := await self.replay(key)) is not None:
            return first
        try:
            plan = await self.plan(action_key, raw)
        except JobsError as e:
            raise registry.Refused([f"Cloud Run can't say what is running: {e}"]) from e
        return await self._start(actor, key, plan, override)

    async def _start(self, actor: str, key: str, plan: registry.Plan, override: str | None,
                     extra: Callable[[Any, list[dict]], None] | None = None, answer: dict | None = None) -> dict:  # fmt: skip
        if plan.window and not override:
            plan.refusals.append(f"now is inside {plan.window}: give a reason to override")
        if override:
            override = reason_of(override, "the override's reason")
        if plan.refusals:
            await asyncio.to_thread(self.control.audit_now, actor, "job_start", plan.action.key, "refused",
                                    {"reasons": plan.refusals, "params": plan.params})  # fmt: skip
            emit(action="job_start", subject=plan.action.key, outcome="refused")
            raise registry.Refused(plan.refusals, overridable=bool(plan.window) and len(plan.refusals) == 1)
        return await asyncio.to_thread(self._start_sync, actor, key, plan, override, extra, answer or {})

    def _start_sync(self, actor: str, key: str, plan: registry.Plan, override: str | None,
                    extra: Callable[[Any, list[dict]], None] | None, answer: dict) -> dict:  # fmt: skip
        """Start each step and record it, in one transaction held across the starts: if the database can't record
        them, the transaction fails before the first start. A step Cloud Run refuses is recorded as failed and the
        rest still start (a full crawl is five jobs)."""
        with self.control.tx() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (key,))
            if (first := self.control.claim(cur, key)) is not None:
                return first
            runs, errors = [], []
            for s in plan.steps:
                run_id = new_id("r")
                try:
                    ex = self.jobs.run(s.job, s.args)
                except JobsError as e:
                    errors.append(f"{s.job}: {e}")
                    continue
                run = {"run_id": run_id, "action": plan.action.key, "job": s.job, "args": s.args,
                       "mode": plan.params.get("mode"), "week": plan.params.get("week"),
                       "test_id": plan.params.get("test_id"), "roaster_id": plan.params.get("roaster"),
                       "execution": ex.name, "actor": actor, "override_reason": override}  # fmt: skip
                self.control.record_run(cur, run)
                runs.append(run)
            if extra and runs:
                extra(cur, runs)
            outcome = "ok" if not errors else "failed"
            self.control.audit(cur, actor, "job_start", plan.action.key, outcome,
                               {"runs": [{k: r[k] for k in ("run_id", "job", "args", "execution")} for r in runs],
                                "errors": errors, "params": plan.params, "override": override,
                                "window": plan.window})  # fmt: skip
            out = {"ok": not errors, "runs": [{"run_id": r["run_id"], "job": r["job"],
                                               "execution": str(r["execution"]).rsplit("/", 1)[-1]} for r in runs],
                   "errors": errors} | answer  # fmt: skip
            if runs:  # a start where nothing started can be retried with the same key
                self.control.remember(cur, key, out)
        emit(action="job_start", subject=plan.action.key, outcome=outcome, runs=[r["run_id"] for r in runs])
        return out

    async def cancel(self, actor: str, key: str, run_id: str) -> dict:
        await self.ready()

        def go():
            runs = {r["run_id"]: r for r in self.control.runs(200)}
            run = runs.get(run_id)
            if run is None or not run["execution"]:
                raise ValueError("no such run")
            with self.control.tx() as cur:
                cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (key,))
                if (first := self.control.claim(cur, key)) is not None:
                    return first
                self.jobs.cancel(run["execution"])
                self.control.audit(cur, actor, "job_cancel", run["job"], "ok", {"run_id": run_id,
                                                                                 "execution": run["execution"]})  # fmt: skip
                out = {"ok": True, "run_id": run_id}
                self.control.remember(cur, key, out)
            emit(action="job_cancel", subject=run["job"], outcome="ok")
            return out

        return await asyncio.to_thread(go)

    async def runs(self, limit: int = 30) -> list[dict]:
        """The runs the admin started, newest first, with their live status from Cloud Run (a final status is
        recorded the first time it is seen), and a test crawl's answer from job_manifests."""
        await self.ready()
        rows = await asyncio.to_thread(self.control.runs, limit)

        def live():
            for r in rows:
                r["status"], r["log_uri"], r["message"], r["duration_s"] = (
                    r["final"] or "unknown",
                    None,
                    None,
                    None,
                )
                if not r["execution"]:
                    continue
                try:
                    e = self.jobs.execution(r["execution"])
                except (JobsError, ValueError) as err:
                    r["message"] = str(err)
                    continue
                r["status"], r["log_uri"], r["message"], r["duration_s"] = (
                    e.status,
                    e.log_uri,
                    e.message,
                    e.duration_s,
                )
                if not r["final"] and e.status in ("succeeded", "failed", "cancelled"):
                    self.control.run_status(r["run_id"], e.status)
                    r["final"] = e.status

        await asyncio.to_thread(live)
        tests = [r["test_id"] for r in rows if r["test_id"]]
        if tests:
            answers = await self.read(queries.test_results, tests)
            for r in rows:
                r["answer"] = answers.get(r["test_id"]) if r["test_id"] else None
        return rows

    # --- decisions -------------------------------------------------------------------------------------------------

    async def decide(self, actor: str, key: str, body: dict) -> dict:
        await self.ready()
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ValueError("no products chosen")
        kind = str(body.get("kind") or "coffee_id")
        raw_seen = body.get("seen")
        seen: dict = raw_seen if isinstance(raw_seen, dict) else {}
        reason = reason_of(body.get("reason"))
        out = await asyncio.to_thread(self.control.decide, actor, key, str(body.get("action")), kind, reason,
                                      [{k: (str(v)[:300] if v is not None else None) for k, v in i.items()}
                                       for i in items], {str(k): (str(v) if v else None) for k, v in seen.items()})  # fmt: skip
        emit(action="decide", subject=out.get("group") or (out.get("decisions") or [""])[0], outcome="ok",
             count=out.get("count"))  # fmt: skip
        return out

    async def undo(self, actor: str, key: str, target: str, reason: str | None) -> dict:
        await self.ready()
        out = await asyncio.to_thread(self.control.undo, actor, key, target, str(reason or "undo"))
        emit(action="undo", subject=target, outcome="ok", count=out.get("count"))
        return out

    # --- release: draft, publish, roll back, discard ---------------------------------------------------------------

    async def pending(self) -> dict:
        """The effective decisions against the served build's: what a draft would change."""
        await self.ready()
        effective = await asyncio.to_thread(self.control.effective)
        served = await self.read(queries.served)
        pinned = await self.read(queries.build_snapshot, served["build_id"]) if served else None
        before = {
            (d["subject_kind"], d["subject"], d["action"])
            for d in ((pinned or {}).get("doc") or {}).get("decisions", [])
        }
        now = {(d["subject_kind"], d["subject"], d["action"]) for d in effective}
        return {"effective": effective, "added": len(now - before), "removed": len(before - now),
                "served": served, "served_snapshot": pinned and pinned["snapshot_id"]}  # fmt: skip

    async def build_draft(self, actor: str, key: str) -> dict:
        if (first := await self.replay(key)) is not None:
            return first
        p = await self.pending()
        if not (p["added"] or p["removed"]):
            raise ValueError("Nothing to build: the decisions are the served release's")
        sid, did = new_id("s").lower(), new_id("draft").lower()
        doc = snap.snapshot(sid, self.now().strftime("%Y-%m-%dT%H:%M:%SZ"), p["effective"])
        digest = snap.digest(doc)
        baseline = (p["served"] or {}).get("build_id")
        action = registry.ACTIONS["draft"]
        plan = await self._guarded(action, {"draft_id": did, "snapshot_id": sid, "sha256": digest})
        # The snapshot first: immutable, so a draft that never starts leaves an unused file, never a changed one.
        if not await asyncio.to_thread(self.snapshots.create, snap.key(sid), doc):
            raise ValueError("snapshot id already used: try again")

        def record(cur, runs):
            self.control.create_snapshot(cur, sid, digest, len(doc["decisions"]), actor)
            self.control.create_draft(cur, did, sid, baseline, runs[0]["run_id"], actor)

        return await self._start(actor, key, plan, None, record, {"draft_id": did, "snapshot_id": sid})

    async def readiness(self, draft: dict, rel: dict) -> Readiness:
        """Publish is disabled until: the draft build passed; the served release is still its baseline; any
        required evaluation is attached; the owner typed the count when more than 50 public rows leave; and each
        store a removal comes from answered a live check (a test crawl) in the last 7 days."""
        build = rel["by_draft"].get(draft["draft_id"])
        info = next((b for b in rel["builds"] if b["build_id"] == build), None)
        served = await self.read(queries.served)
        now_id = (served or {}).get("build_id")
        checks = [
            {"name": "The draft build passed", "ok": bool(info and info["passed"]),
             "why": "not built yet" if not info else ("passed" if info["passed"] else "it failed its checks: see Gate")},
            {"name": "The served release is still its baseline", "ok": now_id == draft["baseline"] or now_id == build,
             "why": f"built against {draft['baseline'] or 'nothing served'}; serving {now_id or 'nothing'}"},
            {"name": "Evaluation attached", "ok": True,
             "why": "not required: decisions on single products, no rule or setting changed (CLAUDE.md step 9)"},
        ]  # fmt: skip
        left = await self.read(queries.leaving, build) if build else {"removed": None}
        removed = left.get("removed")
        excludes = [
            d
            for d in (await self._snapshot_doc(draft["snapshot_id"]))["decisions"]
            if d["action"] == "exclude"
        ]
        roasters = sorted(
            {d.get("roaster_id") or (d.get("coffee_id") or "").split(":")[0] for d in excludes} - {""}
        )
        missing = []
        for r in roasters:
            t = await asyncio.to_thread(self.control.recent_test, r, LIVE_CHECK_DAYS * 24)
            ans = (
                (await self.read(queries.test_results, [t["test_id"]])).get(t["test_id"])
                if t and t.get("test_id")
                else None
            )
            if not (ans and ans.get("ok")):
                missing.append(r)
        checks.append({"name": "Live check of each store a removal comes from", "ok": not missing,
                       "why": (f"{len(roasters)} of {len(roasters)} stores answered a test crawl in the last "
                               f"{LIVE_CHECK_DAYS} days" if not missing else
                               f"no answered test crawl in {LIVE_CHECK_DAYS} days: {', '.join(missing)}"),
                       "missing": missing})  # fmt: skip
        return Readiness(checks, removed, bool(removed and removed > TYPE_OVER))

    async def _snapshot_doc(self, sid: str) -> dict:
        doc = await asyncio.to_thread(self.snapshots.get, snap.key(sid))
        if doc is None:
            raise ValueError(f"snapshot {sid} is missing from the snapshot store")
        return doc

    async def publish(
        self, actor: str, key: str, draft_id: str, typed: str | None, reason: str | None
    ) -> dict:
        if (first := await self.replay(key)) is not None:
            return first
        drafts = {d["draft_id"]: d for d in await asyncio.to_thread(self.control.drafts, 200)}
        draft = drafts.get(draft_id)
        if draft is None:
            raise ValueError("no such draft")
        if draft["last_event"] == "discarded":
            raise registry.Refused(["this draft was discarded"])
        rel = await self.read(queries.releases)
        ready = await self.readiness(draft, rel)
        refusals = [f"{c['name']}: {c['why']}" for c in ready.checks if not c["ok"]]
        if ready.needs_count and str(typed or "").strip() != str(ready.removed):
            refusals.append(f"{ready.removed} public rows leave: type that number to publish")
        if refusals:
            await asyncio.to_thread(
                self.control.audit_now, actor, "publish", draft_id, "refused", {"reasons": refusals}
            )
            raise registry.Refused(refusals)
        build = rel["by_draft"][draft_id]
        params = {
            "build": build,
            "baseline": draft["baseline"],
            "reason": reason_of(reason or f"publish {draft_id}"),
        }
        plan = await self._guarded(registry.ACTIONS["publish"], params)
        return await self._start(actor, key, plan, None,
                                 lambda cur, runs: self.control.draft_event(cur, draft_id, "publish_started", actor,
                                                                            {"build": build, "run": runs[0]["run_id"]}))  # fmt: skip

    async def rollback(self, actor: str, key: str, build: str, reason: str | None) -> dict:
        if (first := await self.replay(key)) is not None:
            return first
        rel = await self.read(queries.releases)
        info = next((b for b in rel["builds"] if b["build_id"] == build), None)
        served = await self.read(queries.served)
        if not info or not info["passed"] or not info["ever_served"]:
            raise registry.Refused(["only a passing release that was served before can be served again"])
        if served and served["build_id"] == build:
            raise registry.Refused(["it is the release served now"])
        plan = await self._guarded(
            registry.ACTIONS["rollback"], {"build": build, "reason": reason_of(reason)}
        )
        return await self._start(actor, key, plan, None)

    async def discard(self, actor: str, key: str, draft_id: str) -> dict:
        await self.ready()

        def go():
            with self.control.tx() as cur:
                if (first := self.control.claim(cur, key)) is not None:
                    return first
                cur.execute("select 1 from control.drafts where draft_id = %s", (draft_id,))
                if not cur.fetchone():
                    raise ValueError("no such draft")
                self.control.draft_event(cur, draft_id, "discarded", actor)
                self.control.audit(cur, actor, "discard", draft_id, "ok")
                out = {"ok": True}
                self.control.remember(cur, key, out)
                return out

        out = await asyncio.to_thread(go)
        emit(action="discard", subject=draft_id, outcome="ok")
        return out
