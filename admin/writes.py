"""Every write the admin makes (docs/design/admin-registry.md §3, §5.2): start or cancel a pipeline job, decide or
undo an include/exclude, add, remove or undo a keyword filter, review what a filter change excludes, build a draft,
Publish, Roll back, Discard.

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
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from coffeedata import classify, rules
from coffeedata import decisions as snap
from coffeedata.classify import KeywordFilter

from . import jobs as registry
from . import queries
from .catalog import PrefilterReader
from .controlplane import ControlPlane, ControlPlaneError, new_id
from .logs import emit
from .runjobs import Jobs, JobsError, logs_url
from .store import Store

TYPE_OVER = 50  # Publish asks for the count of public rows that leave above this (design §3)
LIVE_CHECK_DAYS = 7
STEP9_LIMIT = 50  # CLAUDE.md step 9: over this many products changed, the full evaluation
# A keyword filter change over STEP9_LIMIT is measured in its draft build against Claude's verdicts (eval_prefilter,
# eval/prefilter/README.md). Publish passes with at most this many products Claude calls coffee left without the
# owner's review. A constant, not a setting: changing it is a code change, reviewed like any rule.
PREFILTER_UNREVIEWED_MAX = 0
PREFILTER_SAMPLE = 20  # the preview's sample of what a filter excludes
PREFILTER_LIST_MAX = 50  # the preview lists at most this many products Claude calls coffee
REASON = re.compile(r"[^\x00-\x1f\x7f]{3,300}")


RUNS_WAIT_S = 10  # Writes.runs: how long the runs' live status may take in all


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
    prefilter: dict | None = (
        None  # the keyword filter change's evaluation and the owner's review, when it has one
    )

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
        if action is None or action.page not in ("jobs", "sources"):
            raise ValueError("no such job action")
        await self.ready()
        today = self.now().date()
        params = registry.checked(action, raw, today, lambda: new_id("t").lower()[:40])
        if (
            action.key == "pull"
        ):  # a counted crawl into a week whose merge is still to come, so the data is kept
            state = await self.read(queries.week_state, params["week"])
            if state["merge_done"]:
                params["week"] = registry.week_of(
                    (date.fromisoformat(params["week"]) + timedelta(days=7)).isoformat()
                )
            params["mode"] = "count"
        plan = await self._guarded(action, params)
        if action.key == "pull":  # only places the owner flagged Roaster
            places = params["roasters"].split(",")
            flags = await asyncio.to_thread(self.control.source_flags, places)
            plan.refusals += [f"{d} isn't flagged Roaster: flag it first" for d in places
                              if (flags.get(d) or {}).get("flag") != "roaster"]  # fmt: skip
        return plan

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
            with self.control.once(key) as (cur, first):
                if first is not None:
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
                                    "test_id": plan.params.get("test_id"),
                                    "roaster_id": plan.params.get("roaster") or plan.params.get("roasters"),
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
                with self.control.once(key) as (cur, first):
                    if first is not None:
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

        def live(r: dict) -> None:
            try:
                e = self.jobs.execution(r["execution"])
            except (JobsError, ValueError) as err:
                r["message"] = str(err)
                return
            r["status"], r["message"], r["duration_s"] = e.status, e.message, e.duration_s
            r["log_uri"] = e.log_uri or r["log_uri"]
            if e.status in ("succeeded", "failed", "cancelled"):
                self.control.run_status(r["run_id"], e.status, e.duration_s, e.message)
                r["final"] = e.status

        for r in rows:
            r["status"], r["log_uri"] = r["final"] or "unknown", logs_url(r["execution"] or "")
        # The unfinished runs' live status, together and within RUNS_WAIT_S in all: a slow Cloud Run leaves a run's
        # status "unknown" for this read instead of holding the page past its own deadline.
        todo = [r for r in rows if r["execution"] and not r["final"]]
        if todo:
            _, late = await asyncio.wait([asyncio.ensure_future(asyncio.to_thread(live, r)) for r in todo],
                                            timeout=RUNS_WAIT_S)  # fmt: skip
            for task in late:
                task.cancel()
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

    async def check_settings(self, body: dict) -> dict:
        """What saving these settings would change, before it is saved: the changes, those that decide, and the
        evaluations they need at Publish (a preview acts on nothing)."""
        await self.ready()
        now = await asyncio.to_thread(self.control.settings)
        new = rules.check(_settings_doc(body, now["doc"]))
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
        now = await asyncio.to_thread(self.control.settings)
        out = await asyncio.to_thread(self.control.save_settings, actor, key, _settings_doc(body, now["doc"]),
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

    # --- keyword filters ------------------------------------------------------------------------------------------

    async def prefilter_check(self, body: dict) -> dict:
        """The preview a filter change needs before it can be saved: what it newly excludes in the served release
        (or gives back, for a removal), how many of those are public coffees now, which Claude calls coffee, and
        CLAUDE.md step 9's running total with it. Returns `token`, which the save must carry (a preview acts on
        nothing)."""
        await self.ready()
        event, f, based_on = _filter_change(body)
        now = await asyncio.to_thread(self.control.keyword_filters)
        live = classify.keyword_filters(now["filters"])
        served = await self.read(queries.served)
        refusals: list[str] = []
        if now["version"] != based_on:
            refusals.append("the keyword filters changed since the page was loaded: reload and try again")
        if event == "added" and f in live:
            refusals.append(f"“{f.phrase}” is already a filter on {_field(f.field)}")
        if event == "removed" and f not in live:
            refusals.append(f"“{f.phrase}” isn't a filter on {_field(f.field)}")
        if not served or self.catalog is None:
            return {"lines": ["Nothing is served yet, so there is nothing to preview the filter against."],
                    "refusals": refusals + ["nothing to preview against"]}  # fmt: skip
        build = served["build_id"]
        reader = PrefilterReader(self.catalog, build)
        if f.field == "tags" and not await self.read(lambda _l, _c: reader.has_tags()):
            refusals.append(f"the served release {build} has no tags to preview a filter on tags against: publish a "
                            "build made with this change first")  # fmt: skip
        new = tuple(sorted(set(live) | {f})) if event == "added" else tuple(x for x in live if x != f)
        rows = await self.read(lambda _l, _c: reader.changed(live, new))
        rel = await self.read(queries.releases)
        total, last = await asyncio.to_thread(self.control.step9_total, rel["ever_served"])
        coffee = [r for r in rows if r["verdict_label"] == "coffee"]
        # What changes publicly: a public coffee a new filter takes out, or a product a removed filter gives back that
        # Claude calls coffee (one with no verdict waits for Claude, then counts in its own draft).
        moved = [r for r in rows if r["public"]] if event == "added" else coffee
        in_all = total + len(moved)
        over = in_all > STEP9_LIMIT
        since = f"since the evaluation {last['path']}" if last else "since the running total started"
        name = f"“{f.phrase}” in {_field(f.field)}"
        claude = _claude_says(len(coffee), len(rows))
        if event == "added":
            lines = [f"{name} takes {_n(len(rows), 'product')} out of the served release {build}"
                     + (f": {_n(len(moved), 'public coffee')} {'leaves' if len(moved) == 1 else 'leave'} the "
                        f"catalog. {claude}." if rows else ".")]  # fmt: skip
        else:
            unknown = sum(r["verdict_label"] is None for r in rows)
            lines = [f"Removing {name} gives {_n(len(rows), 'product')} back to Claude and the rules"
                     + (f". {claude}: {'it comes' if len(coffee) == 1 else 'they come'} back to the catalog."
                        if coffee else ".")
                     + (f" {_n(unknown, 'product')} {'has' if unknown == 1 else 'have'} no verdict yet: Claude "
                        "decides next week." if unknown else "")]  # fmt: skip
        changed_all = f"CLAUDE.md step 9: {_n(in_all, 'public coffee')} changed in all {since}"
        if not over:
            lines.append(f"{changed_all}, 50 or fewer, so no full evaluation."
                         + (" Check each product below before you save." if moved else ""))  # fmt: skip
        else:
            lines.append(f"{changed_all}, over 50. You can save it, and the draft build measures it against "
                         "Claude’s verdicts. Publish then waits until you have marked each product Claude calls "
                         "coffee, on Release.")  # fmt: skip
        lines.append("Nothing changes for anyone until a draft of it is published (Release).")
        groups = []
        if coffee:
            groups.append({"title": f"Claude calls {_n(len(coffee), 'product')} coffee", "items": coffee[:PREFILTER_LIST_MAX],
                           "more": max(0, len(coffee) - PREFILTER_LIST_MAX)})  # fmt: skip
        shown = {r["coffee_id"] for r in coffee[:PREFILTER_LIST_MAX]}
        if not over and moved:
            rest = [r for r in moved if r["coffee_id"] not in shown]
            if rest:
                groups.append({"title": "Public coffees it changes: check each", "items": rest, "more": 0})
                shown |= {r["coffee_id"] for r in rest}
        sample = [r for r in rows if r["coffee_id"] not in shown][:PREFILTER_SAMPLE]
        if sample:
            groups.append({"title": f"A sample of what it {'excludes' if event == 'added' else 'gives back'}",
                           "items": sample, "more": 0})  # fmt: skip
        return {"lines": lines, "refusals": refusals, "groups": groups, "token": _preview_token(event, f, based_on, build),
                "count": len(rows), "public": len(moved), "claude_coffee": len(coffee), "in_all": in_all,
                "over": over}  # fmt: skip

    async def prefilter_save(self, actor: str, key: str, body: dict) -> dict:
        """Add or remove a filter. Refused without the preview of exactly this change against the release served
        now (`previewed`: the preview's token)."""
        await self.ready()
        event, f, based_on = _filter_change(body)
        served = await self.read(queries.served)
        want = _preview_token(event, f, based_on, (served or {}).get("build_id") or "")
        if not served or body.get("previewed") != want:
            raise ValueError(
                "Preview this filter first: the preview shows what it excludes before it is saved"
            )
        out = await asyncio.to_thread(self.control.change_filter, actor, key, event, f.phrase, f.field,
                                      reason_of(body.get("reason")), based_on)  # fmt: skip
        written(action="keyword_filter", subject=f"{f.field}:{f.phrase}", outcome="ok", change=event)
        return out

    async def prefilter_undo(self, actor: str, key: str, change: str) -> dict:
        await self.ready()
        if not change.isdigit():
            raise ValueError("bad filter change")
        out = await asyncio.to_thread(self.control.undo_filter, actor, key, int(change))
        written(action="keyword_filter_undo", subject=f"{out['field']}:{out['phrase']}", outcome="ok")
        return out

    async def prefilter_review(self, actor: str, key: str, body: dict) -> dict:
        """Mark a product a draft's filter change newly excludes while Claude calls it coffee: keep_excluded or
        drop_filter. Only a product the draft build's eval_prefilter lists can be marked."""
        await self.ready()
        draft_id = str(body.get("draft_id") or "")
        draft = next(
            (d for d in await asyncio.to_thread(self.control.drafts, 200) if d["draft_id"] == draft_id), None
        )
        if draft is None:
            raise ValueError("no such draft")
        rel = await self.read(queries.releases)
        build = rel["by_draft"].get(draft_id)
        if not build:
            raise ValueError("the draft isn't built yet")
        ev = await self.read(queries.prefilter_eval, build)
        item = next((c for c in ev["coffee"] if c["coffee_id"] == body.get("coffee_id")), None)
        if item is None:
            raise ValueError(
                "that product isn't one the draft's filters newly exclude while Claude calls it coffee"
            )
        digest = _filters_digest(await self._snapshot_doc(draft["snapshot_id"]))
        out = await asyncio.to_thread(
            self.control.review, actor, key, digest, draft_id, item, str(body.get("mark"))
        )
        written(action="prefilter_review", subject=item["coffee_id"], outcome="ok", mark=out["mark"])
        return out

    # --- the source list's flags ---------------------------------------------------------------------------------

    async def flag_sources(self, actor: str, key: str, body: dict) -> dict:
        await self.ready()
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ValueError("no places chosen")
        places = [str(i.get("roaster_id") or "") for i in items]
        seen = {str(i.get("roaster_id")): (str(i["seen"]) if i.get("seen") else None) for i in items}
        coffees: list[dict] = []
        if (
            body.get("flag") == "not_roaster" and self.catalog is not None
        ):  # their coffees leave the catalog too
            served = await self.read(queries.served)
            if served:
                coffees = await self.read(lambda _l, _c: self.catalog.of_roasters(served["build_id"], places))
        out = await asyncio.to_thread(
            self.control.flag_sources, actor, key, str(body.get("flag")), places, seen, coffees
        )
        written(action="source_flag", subject=out["batch_id"], outcome="ok", count=out["count"])
        return out

    async def undo_flags(self, actor: str, key: str, batch: str) -> dict:
        await self.ready()
        out = await asyncio.to_thread(self.control.undo_flags, actor, key, batch)
        written(action="source_flag_undo", subject=batch, outcome="ok", count=out["count"])
        return out

    # --- release: draft, publish, roll back, discard ---------------------------------------------------------------

    async def pending(self, served: dict | None = None, drafts: list[dict] | None = None) -> dict:
        """The effective decisions, rule settings, crawl flags and source-list roasters against the served build's
        snapshot: what a draft would change. `served` and `drafts`: what the caller already read (Release reads each once)."""
        await self.ready()
        effective, settings, states, flags, kf = await asyncio.gather(
            asyncio.to_thread(self.control.effective),
            asyncio.to_thread(self.control.settings),
            asyncio.to_thread(self.control.roaster_states),
            asyncio.to_thread(self.control.source_flags),
            asyncio.to_thread(self.control.keyword_filters),
        )
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
        prefilter = classify.keyword_filters(kf["filters"])
        prefilter_changed = sorted(x.name for x in set(prefilter) ^ set(snap.prefilter(served_doc)))
        # Flags on the source list: places flagged roaster join the registry; registry roasters (which inherit
        # roaster) flagged not a roaster leave it.
        # `known`: the registry without the flags (the served release's, less the places its snapshot added), so a
        # published place stays in the next draft. With nothing served, every not-roaster flag is kept.
        # No flag at all: no read of the release. A served release without its registry fails closed: the served
        # snapshot's flags are kept, and a draft is refused until the registry can be read.
        sources_error = None
        known: set[str] = set()
        if flags and served:
            got = await self.read(queries.registry_ids, served["build_id"])
            if got is None:
                sources_error = (f"The served release {served['build_id']}'s registry (dim_roaster) can't be read, "
                                 "so the source flags can't be applied: try again shortly")  # fmt: skip
            # the registry without the served flags: less the places it added, plus the roasters it dropped
            known = ((got or set()) - set(snap.source_roasters(served_doc))) | set(
                snap.not_roasters(served_doc)
            )
        if sources_error:
            sources, dropped = snap.source_roasters(served_doc), snap.not_roasters(served_doc)
        else:
            sources = sorted(p for p, f in flags.items() if f["flag"] == "roaster" and p not in known)
            dropped = sorted(
                p for p, f in flags.items() if f["flag"] == "not_roaster" and (p in known or not served)
            )
        sources_changed = sorted((set(sources) ^ set(snap.source_roasters(served_doc)))
                                 | (set(dropped) ^ set(snap.not_roasters(served_doc))))  # fmt: skip
        mine = (now, now_settings, set(off), sources, dropped, prefilter)
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
                        snap.settings_of(got), snap.crawl_off(got), snap.source_roasters(got),
                        snap.not_roasters(got), snap.prefilter(got)) == mine:  # fmt: skip
                drafted = d["draft_id"]
            break
        return {"effective": effective, "added": len(now - before), "removed": len(before - now),
                "settings": doc, "settings_version": settings["version_id"], "settings_changed": settings_changed,
                "crawl_off": off, "crawl_changed": crawl_changed, "sources": sources, "not_roasters": dropped,
                "sources_changed": sources_changed, "sources_error": sources_error,
                "prefilter": [x.as_dict() for x in prefilter], "prefilter_changed": prefilter_changed,
                "served": served, "served_snapshot": pinned and pinned["snapshot_id"], "drafted": drafted}  # fmt: skip

    async def build_draft(self, actor: str, key: str, override: str | None = None) -> dict:
        p = await self.pending()
        if p["sources_error"]:
            raise registry.Refused([p["sources_error"]])
        if not (p["added"] or p["removed"] or p["settings_changed"] or p["crawl_changed"] or p["sources_changed"]
                or p["prefilter_changed"]):  # fmt: skip
            raise ValueError("Nothing to build: the decisions, rule settings, keyword filters, crawl flags and source "
                             "flags are the served release's")  # fmt: skip
        sid, did = new_id("s").lower(), new_id("draft").lower()
        doc = snap.snapshot(sid, self.now().strftime("%Y-%m-%dT%H:%M:%SZ"), p["effective"], p["settings"],
                            [{"roaster_id": r, "crawl": False} for r in p["crawl_off"]], p["sources"],
                            p["not_roasters"], p["prefilter"])  # fmt: skip
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
        # Step 9 counts the public coffees the draft's decisions and keyword filters change: rule settings change no
        # public output.
        left = await self.read(queries.leaving, build, effect["coffees"]) if build else {"removed": None}
        removed, changed = left.get("removed"), left.get("changed")
        total, last = await asyncio.to_thread(self.control.step9_total, rel["ever_served"])
        since = f"since the evaluation {last['path']}" if last else "since the total was started"
        prefilter = None
        if effect["filters_changed"]:
            moved = (
                (await self.read(queries.leaving, build, effect["filter_coffees"])).get("changed")
                if build
                else None
            )
            over = bool(moved) and changed is not None and total + changed + moved > STEP9_LIMIT
            prefilter = await self._prefilter_gate(draft, build, moved, over)
            # Over 50, the filter change is measured on its own (eval_prefilter): its products aren't added to the
            # running total, which the next full evaluation of the classifier restarts. At 50 or fewer they count.
            if changed is not None and moved is not None and not over:
                changed += moved
            checks.append(prefilter["check"])
        checks.append({"name": "Evaluation (CLAUDE.md step 9)",
                       "ok": changed is None or total + changed <= STEP9_LIMIT,
                       "why": ("not built yet" if changed is None else
                               f"not required: this draft changes {changed} public coffees, {total + changed} in all "
                               f"{since} (50 or fewer: the short path)" if total + changed <= STEP9_LIMIT else
                               f"this draft changes {changed} public coffees, {total + changed} in all {since}: over "
                               "50, so the full evaluation comes first, then record it below"),
                       "needs_evaluation": changed is not None and total + changed > STEP9_LIMIT})  # fmt: skip
        # CLAUDE.md: a settings change to how a rule decides needs its full evaluation, whatever it counts.
        deciding, needed = effect["deciding"], effect["needs_evaluation"]
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
        # Only a test of the store itself: a connector-wide test's end row counts the connector's fetches, not
        # whether this store answered (its per-store rows stay in the test's own partition).
        stores = {t["roaster_id"] for t in ok if t["roaster_id"]}
        missing = [r for r, _conn in effect["removal_roasters"] if r not in stores]
        n = len(effect["removal_roasters"])
        checks.append({"name": "Live check of each store a removal comes from", "ok": not missing,
                       "why": ("this draft removes nothing" if not n else
                               f"{n} of {n} stores answered a test crawl of the store in the last "
                               f"{LIVE_CHECK_DAYS} days" if not missing else
                               f"no answered test crawl in {LIVE_CHECK_DAYS} days: {', '.join(missing)}"),
                       "missing": missing})  # fmt: skip
        return Readiness(checks, removed, bool(removed and removed > TYPE_OVER), changed, prefilter)

    async def _prefilter_gate(self, draft: dict, build: str | None, moved: int | None, over: bool) -> dict:
        """The keyword filter check: at 50 or fewer (step 9 with the running total), the short path; over 50, the
        draft build's eval_prefilter must be there, and each product Claude calls coffee marked keep_excluded (at most
        PREFILTER_UNREVIEWED_MAX unmarked), none drop_filter. Returns the check and what the Release page shows."""
        name = "Keyword filters (CLAUDE.md step 9)"
        if build is None or moved is None:
            return {"check": {"name": name, "ok": False, "why": "not built yet"}, "eval": None}
        ev = await self.read(queries.prefilter_eval, build)
        digest = _filters_digest(await self._snapshot_doc(draft["snapshot_id"]))
        marks = await asyncio.to_thread(self.control.reviews, digest) if ev["coffee"] else {}
        for c in ev["coffee"]:
            c["review"] = marks.get(c["coffee_id"])
        out = {"eval": ev, "digest": digest, "over": over, "moved": moved}
        s = ev["summary"]
        said = (f"{s['affected']:,} products newly excluded, of which {s['claude_coffee']:,} Claude calls coffee"
                + (f" (agreement {s['share_not_coffee']:.0%}, Wilson 95% {s['wilson_low']:.0%}–{s['wilson_high']:.0%}"
                   f"{'; ' + format(s['no_verdict'], ',') + ' without a verdict' if s['no_verdict'] else ''})"
                   if s["affected"] else "")) if s else ""  # fmt: skip
        if not over:
            out["check"] = {"name": name, "ok": True,
                            "why": f"{_n(moved, 'public coffee')} {'changes' if moved == 1 else 'change'}, within step 9’s 50 with the running total: the "
                                   "short path (the preview listed them)" + (f". {said}" if said else "")}  # fmt: skip
            return out
        if not ev["recorded"]:
            out["check"] = {"name": name, "ok": False,
                            "why": f"{_n(moved, 'public coffee')} {'changes' if moved == 1 else 'change'}, over 50 with the running total, and the draft "
                                   "build has no evaluation of the filters (eval_prefilter): build the draft again"}  # fmt: skip
            return out
        open_ = [c for c in ev["coffee"] if not c["review"]]
        dropped = [c for c in ev["coffee"] if c["review"] and c["review"]["mark"] == "drop_filter"]
        ok = len(open_) <= PREFILTER_UNREVIEWED_MAX and not dropped
        why = f"over 50 with the running total, so measured against Claude’s verdicts: {said}"
        if dropped:
            why += (f". You marked {_n(len(dropped), 'product')} “drop the filter”: remove the filter that excludes "
                    f"{'it' if len(dropped) == 1 else 'them'} on the Queue, then build a new draft")  # fmt: skip
        elif open_:
            why += f". Mark each product Claude calls coffee below: {len(open_):,} still to review"
        else:
            why += ". Each product Claude calls coffee is marked keep excluded" if ev["coffee"] else ""
        out["check"] = {"name": name, "ok": ok, "why": why}
        return out

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
        # The keyword filters: the baseline's products whose answer the filter change turns (catalog.PrefilterReader)
        old_kf, new_kf = snap.prefilter((pinned or {}).get("doc")), snap.prefilter(now_doc)
        filter_coffees: list[str] = []
        if old_kf != new_kf and draft["baseline"] and self.catalog is not None:
            reader = PrefilterReader(self.catalog, draft["baseline"])
            filter_coffees = sorted(
                {r["coffee_id"] for r in await self.read(lambda _l, _c: reader.changed(old_kf, new_kf))}
            )
        roasters = sorted({(m["roaster_id"], m.get("platform")) for d in removing for m in covered(d) if m["roaster_id"]},
                          key=lambda x: (x[0], x[1] or ""))  # fmt: skip
        return {"coffees": coffees, "removal_roasters": roasters, "filter_coffees": filter_coffees,
                "filters_changed": old_kf != new_kf,
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
            with self.control.once(key) as (cur, first):
                if first is not None:
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


def _settings_doc(body: dict, current: dict) -> object:
    """The settings an edit sends: the Queue's plain controls (`param:<rule>:<name>` fields: a whole number, or a
    comma-separated list), applied to the current version; or a whole document (`doc`, the API's), as text or
    parsed. The controls change params only: a family, a polarity or readiness changes how a rule decides, which
    needs its full evaluation first (rules.EVALUATED), so it is never a form field."""
    fields = {k: v for k, v in body.items() if k.startswith("param:")}
    if fields:
        doc = rules.check(current, strict=False)
        by_id = {r["id"]: r for r in doc["rules"]}
        for k, v in fields.items():
            _, rid, name = (k.split(":") + ["", ""])[:3]
            spec = rules.RULE_TYPES.get(rid) and rules.RULE_TYPES[rid].params.get(name)
            if not spec:
                raise ValueError(f"no setting {rid} {name}")
            text = str(v).strip()
            if spec.kind is int:
                if not re.fullmatch(r"\d{1,6}", text):
                    raise ValueError(f"{rid} {name.replace('_', ' ')} must be a whole number")
                by_id[rid]["params"][name] = int(text)
            else:
                by_id[rid]["params"][name] = [x.strip() for x in text.split(",") if x.strip()]
        return doc
    raw = body.get("doc")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"the settings aren't valid JSON: {e.msg} (line {e.lineno})") from e
    return raw


def _field(field: str) -> str:
    return {"title": "titles", "product_type": "product types", "tags": "tags"}[field]


def _claude_says(coffee: int, of: int) -> str:
    """'Claude calls 1 of them coffee', in words that read right for none, one and all."""
    them = "it" if of == 1 else "them"
    if coffee == 0:
        return f"Claude calls none of {them} coffee" if of > 1 else "Claude doesn’t call it coffee"
    if coffee == of:
        return f"Claude calls {'it' if of == 1 else 'all of them'} coffee"
    return f"Claude calls {coffee:,} of {them} coffee"


def _n(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _filter_change(body: dict) -> tuple[str, KeywordFilter, int | None]:
    """A filter change from a form: the event (added or removed), the filter (classify checks it), and the version the
    page showed."""
    event = {"add": "added", "remove": "removed"}.get(str(body.get("event") or ""))
    if event is None:
        raise ValueError("add or remove a filter")
    f = KeywordFilter(str(body.get("phrase") or ""), str(body.get("field") or ""))
    raw = str(body.get("based_on") or "")
    if raw and not raw.isdigit():
        raise ValueError("bad filters version")
    return event, f, int(raw) if raw else None


def _preview_token(event: str, f: KeywordFilter, based_on: int | None, build: str) -> str:
    """What the preview of one change against one served release returns, and its save must carry back."""
    return hashlib.sha256(json.dumps([event, f.field, f.phrase, based_on, build]).encode()).hexdigest()[:32]


def _filters_digest(doc: dict | None) -> str:
    """The digest of a snapshot's keyword filters: the owner's review marks are kept per filter set."""
    return hashlib.sha256(json.dumps([f.as_dict() for f in snap.prefilter(doc)]).encode()).hexdigest()
