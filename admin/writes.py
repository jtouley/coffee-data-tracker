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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from coffeedata import decisions as snap
from coffeedata import rules

from . import jobs as registry
from . import queries
from .controlplane import ControlPlane, ControlPlaneError, new_id
from .logs import emit
from .runjobs import Jobs, JobsError, logs_url
from .store import Store

TYPE_OVER = 50  # Publish asks for the count of public rows that leave above this (design §3)
LIVE_CHECK_DAYS = 7
STEP9_LIMIT = 50  # CLAUDE.md step 9: over this many products changed, the full evaluation
REASON = re.compile(r"[^\x00-\x1f\x7f]{3,300}")


class StartedUnrecorded(Exception):
    """Cloud Run started (or cancelled) these executions, but the control plane couldn't record it, twice."""

    def __init__(self, executions: list[str], verb: str = "started"):
        super().__init__(", ".join(e.rsplit("/", 1)[-1] for e in executions))
        self.executions, self.verb = executions, verb


def written(**fields) -> None:
    """The structured line every write logs (event admin_write), which the owner's alert emails."""
    emit("NOTICE", event="admin_write", **fields)


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
    changed: int | None = None

    @property
    def ready(self) -> bool:
        return all(c["ok"] for c in self.checks)


class Writes:
    def __init__(self, control: ControlPlane, jobs: Jobs, snapshots: Store,
                 read: Callable[..., Awaitable[Any]], now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 catalog: Any = None):  # fmt: skip
        self.control, self.jobs, self.snapshots, self.read, self.now = control, jobs, snapshots, read, now
        self.catalog = catalog  # admin.catalog.Catalog: who a decision by version covers
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
        try:
            running = await asyncio.to_thread(self._running, {s.job for s in steps})
        except JobsError as e:  # every start, Draft, Publish and Roll back alike: a refusal, audited
            plan.refusals.append(f"Cloud Run can't say what is running: {e}")
            running = {}
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
        """Start an action's jobs (main.write has already returned the first answer of a used key)."""
        return await self._start(actor, key, await self.plan(action_key, raw), override)

    async def _check(self, actor: str, plan: registry.Plan, override: str | None) -> str | None:
        """The guards' verdict on a plan: refused (audited) with every reason, or the checked override reason."""
        if plan.window and not override:
            plan.refusals.append(f"now is inside {plan.window}: give a reason to override")
        if override:
            override = reason_of(override, "the override's reason")
        if plan.refusals:
            await asyncio.to_thread(self.control.audit_now, actor, "job_start", plan.action.key, "refused",
                                    {"reasons": plan.refusals, "params": plan.params})  # fmt: skip
            written(action="job_start", subject=plan.action.key, outcome="refused")
            raise registry.Refused(plan.refusals, overridable=bool(plan.window) and len(plan.refusals) == 1)
        return override

    async def _start(self, actor: str, key: str, plan: registry.Plan, override: str | None,
                     extra: Callable[[Any, list[dict]], None] | None = None, answer: dict | None = None) -> dict:  # fmt: skip
        override = await self._check(actor, plan, override)
        try:
            return await asyncio.to_thread(self._start_sync, actor, key, plan, override, extra, answer or {})
        except registry.Refused as e:  # the overlap check again, under the jobs' locks: another start won
            await asyncio.to_thread(self.control.audit_now, actor, "job_start", plan.action.key, "refused",
                                    {"reasons": e.reasons, "params": plan.params})  # fmt: skip
            raise

    def _start_sync(self, actor: str, key: str, plan: registry.Plan, override: str | None,
                    extra: Callable[[Any, list[dict]], None] | None, answer: dict) -> dict:  # fmt: skip
        """Start each step and record it in one transaction, opened (and so known to work) before the first start.
        Under a lock per job, what is running is checked again, so two submissions (two tabs) can't both start the
        same job. A step Cloud Run refuses is recorded as failed and the rest still start (a full crawl is five jobs).
        If the record can't be committed after Cloud Run started a step, it is written again in a new transaction;
        if that fails too, StartedUnrecorded names the executions (an ERROR line, which alerts) and a retry is
        refused by the overlap check, so nothing starts twice."""
        started: list[dict] = []
        errors: list[str] = []
        try:
            with self.control.tx() as cur:
                cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (key,))
                if (first := self.control.claim(cur, key)) is not None:
                    return first
                jobs = sorted({s.job for s in plan.steps})
                for job in jobs:
                    cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("job:" + job,))
                running = self._running(set(jobs))
                if running:
                    raise registry.Refused([f"{j} is already running ({e}): wait for it, or cancel it first"
                                            for j, e in sorted(running.items())])  # fmt: skip
                for s in plan.steps:
                    try:
                        ex = self.jobs.run(s.job, s.args)
                    except JobsError as e:
                        errors.append(f"{s.job}: {e}")
                        continue
                    started.append({"run_id": new_id("r"), "action": plan.action.key, "job": s.job, "args": s.args,
                                    "mode": plan.params.get("mode"), "week": plan.params.get("week"),
                                    "test_id": plan.params.get("test_id"), "roaster_id": plan.params.get("roaster"),
                                    "execution": ex.name, "actor": actor, "override_reason": override})  # fmt: skip
                out = self._record(cur, actor, key, plan, override, started, errors, extra, answer)
        except ControlPlaneError:
            if not started:
                raise
            try:
                with self.control.tx() as cur:
                    out = self._record(cur, actor, key, plan, override, started, errors, extra, answer)
            except ControlPlaneError as e:
                names = [r["execution"] for r in started]
                emit("ERROR", event="admin_start_unrecorded", action=plan.action.key, executions=names)
                raise StartedUnrecorded(names) from e
        written(action="job_start", subject=plan.action.key, outcome="ok" if not errors else "failed",
                runs=[r["run_id"] for r in started])  # fmt: skip
        return out

    def _record(self, cur, actor: str, key: str, plan: registry.Plan, override: str | None, runs: list[dict],
                errors: list[str], extra: Callable[[Any, list[dict]], None] | None, answer: dict) -> dict:  # fmt: skip
        for run in runs:
            self.control.record_run(cur, run)
        if extra and runs:
            extra(cur, runs)
        self.control.audit(cur, actor, "job_start", plan.action.key, "ok" if not errors else "failed",
                           {"runs": [{k: r[k] for k in ("run_id", "job", "args", "execution")} for r in runs],
                            "errors": errors, "params": plan.params, "override": override,
                            "window": plan.window})  # fmt: skip
        out = {"ok": not errors, "runs": [{"run_id": r["run_id"], "job": r["job"],
                                           "execution": str(r["execution"]).rsplit("/", 1)[-1]} for r in runs],
               "errors": errors} | answer  # fmt: skip
        if runs:  # a start where nothing started can be retried with the same key
            self.control.remember(cur, key, out)
        return out

    async def cancel(self, actor: str, key: str, run_id: str) -> dict:
        """Cancel a run, recorded as starts are: in the transaction around the call, and if that commit fails after
        Cloud Run cancelled it, again in a new one; failing that, the error names the execution."""
        await self.ready()

        def record(cur, run):
            self.control.audit(cur, actor, "job_cancel", run["job"], "ok",
                               {"run_id": run_id, "execution": run["execution"]})  # fmt: skip
            out = {"ok": True, "run_id": run_id}
            self.control.remember(cur, key, out)
            return out

        def go():
            run = self.control.run(run_id)
            if run is None or not run["execution"]:
                raise ValueError("no such run")
            cancelled = False
            try:
                with self.control.tx() as cur:
                    cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (key,))
                    if (first := self.control.claim(cur, key)) is not None:
                        return first
                    self.jobs.cancel(run["execution"])
                    cancelled = True
                    out = record(cur, run)
            except ControlPlaneError:
                if not cancelled:
                    raise
                try:
                    with self.control.tx() as cur:
                        out = record(cur, run)
                except ControlPlaneError as e:
                    emit("ERROR", event="admin_cancel_unrecorded", execution=run["execution"])
                    raise StartedUnrecorded([run["execution"]], "cancelled") from e
            written(action="job_cancel", subject=run["job"], outcome="ok")
            return out

        return await asyncio.to_thread(go)

    async def runs(self, limit: int = 30) -> list[dict]:
        """The runs the admin started, newest first, with their live status from Cloud Run (a final status is
        recorded the first time it is seen), and a test crawl's answer from job_manifests."""
        await self.ready()
        rows = await asyncio.to_thread(self.control.runs, limit)

        def live():
            for r in rows:
                r["status"], r["log_uri"] = r["final"] or "unknown", logs_url(r["execution"] or "")
                if not r["execution"] or r["final"]:  # a finished run's outcome is recorded: no API call
                    continue
                try:
                    e = self.jobs.execution(r["execution"])
                except (JobsError, ValueError) as err:
                    r["message"] = str(err)
                    continue
                r["status"], r["message"], r["duration_s"] = e.status, e.message, e.duration_s
                r["log_uri"] = e.log_uri or r["log_uri"]
                if e.status in ("succeeded", "failed", "cancelled"):
                    self.control.run_status(r["run_id"], e.status, e.duration_s, e.message)
                    r["final"] = e.status

        await asyncio.to_thread(live)
        tests = [r["test_id"] for r in rows if r["test_id"]]
        if tests:
            answers = await self.read(queries.test_results, tests)
            for r in rows:
                conn = (
                    (r["args"] or [])[(r["args"] or []).index("--connector") + 1]
                    if "--connector" in (r["args"] or [])
                    else None
                )
                r["answer"] = (
                    next((a for a in answers.get(r["test_id"], []) if a["connector"] == conn), None)
                    if r["test_id"]
                    else None
                )
        return rows

    # --- decisions -------------------------------------------------------------------------------------------------

    async def decide(self, actor: str, key: str, body: dict) -> dict:
        await self.ready()
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ValueError("no products chosen")
        kind = str(body.get("kind") or "coffee_id")
        # What the page showed as each subject's latest decision (a stale tab conflicts): each item carries it, for
        # its coffee and for its version, so it holds whichever the owner picks in the confirm sheet.
        seen = {str(i.get(kind)): i.get(f"seen_{kind}") for i in items if i.get(kind)}
        reason = reason_of(body.get("reason"))
        out = await asyncio.to_thread(self.control.decide, actor, key, str(body.get("action")), kind, reason,
                                      [{k: (str(v)[:300] if v is not None else None) for k, v in i.items()}
                                       for i in items], {str(k): (str(v) if v else None) for k, v in seen.items()})  # fmt: skip
        written(action="decide", subject=out.get("group") or (out.get("decisions") or [""])[0], outcome="ok",
             count=out.get("count"))  # fmt: skip
        return out

    async def undo(self, actor: str, key: str, target: str, reason: str | None) -> dict:
        await self.ready()
        out = await asyncio.to_thread(self.control.undo, actor, key, target, str(reason or "undo"))
        written(action="undo", subject=target, outcome="ok", count=out.get("count"))
        return out

    # --- rule settings and crawl flags -------------------------------------------------------------------------

    async def check_settings(self, raw: object) -> dict:
        """What saving these settings would change, before it is saved: the changes, those that decide, and the
        evaluations they need at Publish (a preview acts on nothing)."""
        await self.ready()
        now = await asyncio.to_thread(self.control.settings)
        new = rules.check(_settings_doc(raw))
        changed, deciding = rules.changed(now["doc"], new), rules.decides(now["doc"], new)
        needed = rules.evaluation_needed(now["doc"], new)
        lines = [f"Changes: {', '.join(changed)}." if changed else "Nothing changes."]
        if needed:
            lines.append(f"{', '.join(deciding)} changes how a rule decides. Publish will need its full evaluation "
                         f"first (CLAUDE.md; eval/{needed[0]}/), whatever it counts.")  # fmt: skip
        elif changed:
            lines.append("The draft build counts the public products this changes. Over 50 on top of the running "
                         "total, Publish needs the full evaluation (CLAUDE.md step 9).")  # fmt: skip
        lines.append("Nothing changes for anyone until a draft of it is published (Release).")
        return {"lines": lines, "changed": changed, "decides": deciding, "needs_evaluation": needed,
                "refusals": [] if changed else ["nothing changed"]}  # fmt: skip

    async def save_settings(self, actor: str, key: str, body: dict) -> dict:
        await self.ready()
        out = await asyncio.to_thread(self.control.save_settings, actor, key, _settings_doc(body.get("doc")),
                                      body.get("based_on") or None, reason_of(body.get("reason")))  # fmt: skip
        written(action="settings", subject=out["version_id"], outcome="ok", decides=out["decides"])
        return out

    async def set_crawl(self, actor: str, key: str, roaster_id: str, body: dict) -> dict:
        await self.ready()
        crawl = body.get("crawl")
        if crawl not in ("on", "off"):
            raise ValueError("crawl is on or off")
        seen = body.get("seen")
        out = await asyncio.to_thread(self.control.set_crawl, actor, key, roaster_id, crawl == "on",
                                      reason_of(body.get("reason")),
                                      None if seen not in ("on", "off") else seen == "on")  # fmt: skip
        written(action="crawl_flag", subject=roaster_id, outcome="ok", crawl=crawl)
        return out

    # --- release: draft, publish, roll back, discard ---------------------------------------------------------------

    async def pending(self, served: dict | None = None, drafts: list[dict] | None = None) -> dict:
        """The effective decisions, rule settings and crawl flags against the served build's snapshot: what a draft
        would change. `served` and `drafts`: what the caller already read (Release reads each once)."""
        await self.ready()
        effective = await asyncio.to_thread(self.control.effective)
        settings = await asyncio.to_thread(self.control.settings)
        states = await asyncio.to_thread(self.control.roaster_states)
        if served is None:
            served = await self.read(queries.served)
        if drafts is None:
            drafts = await asyncio.to_thread(self.control.drafts, 5)
        pinned = await self.read(queries.build_snapshot, served["build_id"]) if served else None
        served_doc = (pinned or {}).get("doc")
        before = {
            (d["subject_kind"], d["subject"], d["action"]) for d in (served_doc or {}).get("decisions", [])
        }
        now = {(d["subject_kind"], d["subject"], d["action"]) for d in effective}
        off = sorted(r for r, s in states.items() if not s["crawl"])
        doc = settings["doc"] if settings["version_id"] else None  # no edit yet: the defaults, left out
        now_settings = snap.settings_of({"settings": doc} if doc else None)
        settings_changed = rules.changed(snap.settings_of(served_doc), now_settings)
        crawl_changed = sorted(set(off) ^ snap.crawl_off(served_doc))
        mine = (now, now_settings, set(off))
        # The latest draft still to review (not discarded or published), if it builds exactly these decisions on the
        # served release: no second draft for the same thing.
        drafted = None
        for d in drafts[:5]:
            if d["last_event"] in ("discarded", "publish_started"):
                continue
            if d["baseline"] != (served or {}).get("build_id"):
                break
            got = await asyncio.to_thread(self.snapshots.get, snap.key(d["snapshot_id"]))
            if got and ({(x["subject_kind"], x["subject"], x["action"]) for x in got["decisions"]},
                        snap.settings_of(got), snap.crawl_off(got)) == mine:  # fmt: skip
                drafted = d["draft_id"]
            break
        return {"effective": effective, "added": len(now - before), "removed": len(before - now),
                "settings": doc, "settings_version": settings["version_id"], "settings_changed": settings_changed,
                "crawl_off": off, "crawl_changed": crawl_changed,
                "served": served, "served_snapshot": pinned and pinned["snapshot_id"], "drafted": drafted}  # fmt: skip

    async def build_draft(self, actor: str, key: str, override: str | None = None) -> dict:
        p = await self.pending()
        if not (p["added"] or p["removed"] or p["settings_changed"] or p["crawl_changed"]):
            raise ValueError(
                "Nothing to build: the decisions, rule settings and crawl flags are the served release's"
            )
        sid, did = new_id("s").lower(), new_id("draft").lower()
        doc = snap.snapshot(sid, self.now().strftime("%Y-%m-%dT%H:%M:%SZ"), p["effective"], p["settings"],
                            [{"roaster_id": r, "crawl": False} for r in p["crawl_off"]])  # fmt: skip
        digest = snap.digest(doc)
        baseline = (p["served"] or {}).get("build_id")
        action = registry.ACTIONS["draft"]
        plan = await self._guarded(action, {"draft_id": did, "snapshot_id": sid, "sha256": digest})
        override = await self._check(actor, plan, override)  # refused before any snapshot is written
        # The snapshot before the start: immutable, so a draft that never starts leaves an unused file, never a
        # changed one.
        if not await asyncio.to_thread(self.snapshots.create, snap.key(sid), doc):
            raise ValueError("snapshot id already used: try again")

        def record(cur, runs):
            self.control.create_snapshot(cur, sid, digest, len(doc["decisions"]), actor)
            self.control.create_draft(cur, did, sid, baseline, runs[0]["run_id"], actor)

        return await self._start(actor, key, plan, override, record, {"draft_id": did, "snapshot_id": sid})

    async def readiness(self, draft: dict, rel: dict, served: dict | None) -> Readiness:
        """Publish is disabled until: the draft build passed; the served release is still its baseline; CLAUDE.md
        step 9's running total stays at 50 or fewer with this draft; the owner typed the count when more than 50
        public rows leave; and each store this draft removes a coffee from answered a live check (a test crawl of the
        store, or of its whole connector) in the last 7 days."""
        build = rel["by_draft"].get(draft["draft_id"])
        info = next((b for b in rel["builds"] if b["build_id"] == build), None)
        now_id = (served or {}).get("build_id")
        checks = [
            {"name": "The draft build passed", "ok": bool(info and info["passed"]),
             "why": "not built yet" if not info else ("passed" if info["passed"] else "it failed its checks: see Gate")},
            {"name": "The served release is still its baseline", "ok": now_id == draft["baseline"] or now_id == build,
             "why": f"built against {draft['baseline'] or 'nothing served'}; serving {now_id or 'nothing'}"},
        ]  # fmt: skip
        effect = await self.effect(draft)
        # CLAUDE.md: a settings change to how a rule decides needs its full evaluation, whatever it counts; any
        # other settings change counts every public coffee the draft changes, on top of step 9's running total.
        deciding, needed, settings_changed = (
            effect["deciding"],
            effect["needs_evaluation"],
            effect["settings_changed"],
        )
        coffees = effect["coffees"]
        if (
            settings_changed and build
        ):  # a settings change moves public output only through what the guard holds
            coffees = sorted(set(coffees) | set(await self.read(queries.held, build))
                             | set(await self.read(queries.held, draft["baseline"])))  # fmt: skip
        left = await self.read(queries.leaving, build, coffees) if build else {"removed": None}
        removed, changed = left.get("removed"), left.get("changed")
        total, last = await asyncio.to_thread(self.control.step9_total, rel["ever_served"])
        since = f"since the evaluation {last['path']}" if last else "since the total was started"
        checks.append({"name": "Evaluation (CLAUDE.md step 9)",
                       "ok": changed is None or total + changed <= STEP9_LIMIT,
                       "why": ("not built yet" if changed is None else
                               f"not required: this draft changes {changed} public coffees, {total + changed} in all "
                               f"{since} (50 or fewer: the short path)" if total + changed <= STEP9_LIMIT else
                               f"this draft changes {changed} public coffees, {total + changed} in all {since}: over "
                               "50, so the full evaluation comes first, then record it below"),
                       "needs_evaluation": changed is not None and total + changed > STEP9_LIMIT})  # fmt: skip
        checks.append({"name": "Evaluation of a rule change", "ok": not needed,
                       "why": ("no rule settings change how a rule decides" if not deciding else
                               f"evaluated: {', '.join(deciding)}" if not needed else
                               f"{', '.join(deciding)} changes how a rule decides: CLAUDE.md asks for its full "
                               f"evaluation first (eval/{needed[0]}/, merged with its entry in "
                               "coffeedata.rules.EVALUATED)")})  # fmt: skip
        tests = await asyncio.to_thread(self.control.tests_since, LIVE_CHECK_DAYS * 24)
        answers = (
            await self.read(queries.test_results, sorted({t["test_id"] for t in tests})) if tests else {}
        )
        ok = [t for t in tests if any(a.get("ok") for a in answers.get(t["test_id"], [])
                                      if a["connector"] == t["connector"])]  # fmt: skip
        stores = {t["roaster_id"] for t in ok if t["roaster_id"]}  # a test of the store itself
        connectors = {t["connector"] for t in ok if not t["roaster_id"]}  # or of its whole connector
        missing = [r for r, conn in effect["removal_roasters"] if r not in stores and conn not in connectors]
        n = len(effect["removal_roasters"])
        checks.append({"name": "Live check of each store a removal comes from", "ok": not missing,
                       "why": ("this draft removes nothing" if not n else
                               f"{n} of {n} stores answered a test crawl (of the store or its connector) in the last "
                               f"{LIVE_CHECK_DAYS} days" if not missing else
                               f"no answered test crawl in {LIVE_CHECK_DAYS} days: {', '.join(missing)}"),
                       "missing": missing})  # fmt: skip
        return Readiness(checks, removed, bool(removed and removed > TYPE_OVER), changed)

    async def effect(self, draft: dict) -> dict:
        """What a draft changes against the release it was built on: the coffees its changed decisions cover (a
        decision by version covers every product sharing it, across roasters), the stores it removes coffees from
        (new excludes, and includes it undoes) with their connector, and its rule settings
        changes (which decide, and the evaluations those still need)."""
        now_doc = await self._snapshot_doc(draft["snapshot_id"])
        pinned = await self.read(queries.build_snapshot, draft["baseline"]) if draft["baseline"] else None
        old_settings, new_settings = snap.settings_of((pinned or {}).get("doc")), snap.settings_of(now_doc)
        before = ((pinned or {}).get("doc") or {}).get("decisions", [])
        key_of = lambda d: (d["subject_kind"], d["subject"], d["action"])  # noqa: E731
        was, now = {key_of(d) for d in before}, {key_of(d) for d in now_doc["decisions"]}
        changed = [d for d in now_doc["decisions"] if key_of(d) not in was] + [
            d for d in before if key_of(d) not in now
        ]
        removing = [d for d in now_doc["decisions"] if d["action"] == "exclude" and key_of(d) not in was]
        removing += [d for d in before if d["action"] == "include" and key_of(d) not in now]
        members: list[dict] = []
        if draft["baseline"] and self.catalog is not None:
            ids = [d["subject"] for d in changed if d["subject_kind"] == "coffee_id"]
            hashes = [d["subject"] for d in changed if d["subject_kind"] == "input_hash"]
            members = await self.read(lambda _l, _c: self.catalog.members(draft["baseline"], ids, hashes))
        by_id = {m["coffee_id"]: m for m in members}
        by_hash: dict[str, list[dict]] = {}
        for m in members:
            by_hash.setdefault(m["input_hash"], []).append(m)

        def covered(d: dict) -> list[dict]:
            if d["subject_kind"] == "input_hash":
                return by_hash.get(d["subject"], [])
            m = by_id.get(d["subject"])
            return [m] if m else [{"coffee_id": d["subject"], "roaster_id": d.get("roaster_id")
                                   or d["subject"].split(":")[0], "platform": None}]  # fmt: skip

        coffees = sorted({m["coffee_id"] for d in changed for m in covered(d)})
        roasters = sorted({(m["roaster_id"], m.get("platform")) for d in removing for m in covered(d) if m["roaster_id"]},
                          key=lambda x: (x[0], x[1] or ""))  # fmt: skip
        return {"coffees": coffees, "removal_roasters": roasters,
                "settings_changed": rules.changed(old_settings, new_settings),
                "deciding": rules.decides(old_settings, new_settings),
                "needs_evaluation": rules.evaluation_needed(old_settings, new_settings)}  # fmt: skip

    async def _snapshot_doc(self, sid: str) -> dict:
        doc = await asyncio.to_thread(self.snapshots.get, snap.key(sid))
        if doc is None:
            raise ValueError(f"snapshot {sid} is missing from the snapshot store")
        return doc

    async def publish(
        self,
        actor: str,
        key: str,
        draft_id: str,
        typed: str | None,
        reason: str | None,
        override: str | None = None,
    ) -> dict:
        await self.ready()
        drafts = {d["draft_id"]: d for d in await asyncio.to_thread(self.control.drafts, 200)}
        draft = drafts.get(draft_id)
        if draft is None:
            raise ValueError("no such draft")
        if draft["last_event"] == "discarded":
            raise registry.Refused(["this draft was discarded"])
        rel = await self.read(queries.releases)
        ready = await self.readiness(draft, rel, await self.read(queries.served))
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
        return await self._start(actor, key, plan, override,
                                 lambda cur, runs: self.control.draft_event(cur, draft_id, "publish_started", actor,
                                                                            {"build": build, "run": runs[0]["run_id"], "changed": ready.changed}))  # fmt: skip

    async def rollback(
        self, actor: str, key: str, build: str, reason: str | None, override: str | None = None
    ) -> dict:
        await self.ready()
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
        return await self._start(actor, key, plan, override)

    async def evaluation(self, actor: str, key: str, path: str, reason: str | None) -> dict:
        """Record a full evaluation (CLAUDE.md steps 2-7): step 9's running total starts again."""
        await self.ready()
        path = str(path or "").strip()
        if not re.fullmatch(r"eval/[A-Za-z0-9_./-]+", path) or ".." in path:
            raise ValueError("Give the evaluation's folder, like eval/classifier-2026-11/")
        out = await asyncio.to_thread(self.control.record_evaluation, actor, key, path,
                                      reason_of(reason or f"full evaluation {path}"))  # fmt: skip
        written(action="evaluation", subject=path, outcome="ok")
        return out

    async def discard(self, actor: str, key: str, draft_id: str) -> dict:
        await self.ready()
        rel = await self.read(queries.releases)
        draft = next(
            (d for d in await asyncio.to_thread(self.control.drafts, 200) if d["draft_id"] == draft_id), None
        )
        build = rel["builds"] and next(
            (b for b in rel["builds"] if b["build_id"] == rel["by_draft"].get(draft_id)), None
        )
        state = ("published" if build and build["ever_served"] else
                 "publishing" if draft and draft["last_event"] == "publish_started" else None)  # fmt: skip

        def go():
            with self.control.tx() as cur:
                cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (key,))
                if (first := self.control.claim(cur, key)) is not None:
                    return first
                cur.execute("select 1 from control.drafts where draft_id = %s", (draft_id,))
                if not cur.fetchone():
                    raise ValueError("no such draft")
                if state in ("publishing", "published"):
                    raise registry.Refused(
                        [f"draft {draft_id} is {state}: roll back instead of discarding it"]
                    )
                self.control.draft_event(cur, draft_id, "discarded", actor)
                self.control.audit(cur, actor, "discard", draft_id, "ok")
                out = {"ok": True}
                self.control.remember(cur, key, out)
                return out

        out = await asyncio.to_thread(go)
        written(action="discard", subject=draft_id, outcome="ok")
        return out


def _settings_doc(raw: object) -> object:
    """The settings an edit sends: a JSON document, as text (the form's) or already parsed."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"the settings aren't valid JSON: {e.msg} (line {e.lineno})") from e
    return raw
