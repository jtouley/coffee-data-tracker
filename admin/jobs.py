"""What the admin can start, in one table (docs/design/admin-registry.md, "Jobs"): every pipeline Cloud Run job the
owner used to start with `gcloud run jobs execute`, with the arguments the CLI takes, and the guards around them.

Adding a job is one `Action` in ACTIONS: its fields (from FIELDS) and a function from the checked fields to the
steps it starts (a Cloud Run job and its argument override). The Jobs page, its confirm step, the API, the guards and
the audit row all come from that entry.

Guards (every start):
- **No overlap:** a job with a running execution is refused (cutover2.sh's step 1 check).
- **Scheduled windows:** refused from Monday 09:00 UTC until the week's merge is done, and on Tuesday 03:00-12:00
  UTC (merge, enrich, build, the site), unless the owner overrides with a reason, which is recorded.
- **A merged week is never crawled again:** "count for week W" is refused once W's merge has a done row.
- **A test crawl of one roaster at most once a day.**
Infra stays on the CLI: nothing here changes what a job is (image, IAM, schedule, Terraform).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from coffeedata.connectors import CONNECTORS, TEST_ID, week_of

ROASTER = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
BUILD = re.compile(r"[0-9A-Za-z-]{1,64}")
CRAWL_JOB = {c: "crawl-" + c.replace("_", "-") for c in sorted(CONNECTORS)}


class Refused(Exception):
    """A start the guards refuse, with every reason (shown on the page, recorded in the audit)."""

    def __init__(self, reasons: list[str], overridable: bool = False):
        super().__init__("; ".join(reasons))
        self.reasons, self.overridable = reasons, overridable


@dataclass(frozen=True)
class Field:
    label: str
    choices: tuple[str, ...] = ()  # a select; empty: free text, checked by `check`
    check: re.Pattern | None = None
    default: str = ""
    help: str = ""


FIELDS = {
    "connector": Field("Connector", tuple(sorted(CONNECTORS))),
    "connectors": Field("Connector", ("all", *sorted(CONNECTORS)), default="all"),
    "roaster": Field("Roaster (its domain)", check=ROASTER, help="e.g. onyxcoffeelab.com"),
    "mode": Field(
        "Mode",
        ("test", "count"),
        default="test",
        help="test writes to its own test=<id> sub-partition, which the merge never reads; count writes "
        "into week W's partition, like the scheduled passes",
    ),  # fmt: skip
    "week": Field("Week (any day of it)", check=re.compile(r"\d{4}-\d{2}-\d{2}"), help="for count mode"),
}


@dataclass(frozen=True)
class Step:
    job: str
    args: list[str] | None  # None: the job's own arguments


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    help: str
    fields: tuple[str, ...]
    steps: Callable[[dict], list[Step]]
    page: str = "jobs"  # jobs: on the Jobs page; release: started from the Release page only
    # the week the start acts on, for the guards: the crawl's (count) or the job's --run-date
    acts_on_week: bool = True


def _mode(p: dict) -> list[str]:
    return ["--test", p["test_id"]] if p["mode"] == "test" else ["--run-date", p["week"]]


def _crawl(p: dict) -> list[Step]:
    cs = sorted(CONNECTORS) if p["connectors"] == "all" else [p["connectors"]]
    return [Step(CRAWL_JOB[c], ["crawl", "--connector", c, *_mode(p)]) for c in cs]


def _sh(script: str) -> list[str]:
    """The build job runs `sh -c <args[0]>`: an override is one script, built here from fixed text and checked ids."""
    return [script]


ACTIONS: dict[str, Action] = {
    a.key: a
    for a in (
        Action("crawl", "Crawl", "Every roaster of one connector, or of all five: a full crawl.",
               ("connectors", "mode", "week"), _crawl),
        Action("crawl-one", "Crawl one roaster", "crawl --only: one store, even if done or quarantined this week. "
               "In test mode it is the live check of a store.", ("connector", "roaster", "mode", "week"),
               lambda p: [Step(CRAWL_JOB[p["connector"]],
                               ["crawl", "--connector", p["connector"], "--only", p["roaster"], *_mode(p)])]),
        Action("crawl-retry", "Shopify retry", "crawl-retry: Shopify's parked stores, from their blocked page.",
               ("mode", "week"),
               lambda p: [Step("crawl-retry", ["crawl-retry", "--connector", "shopify", *_mode(p)])]),
        Action("fetch-health", "Fetch health", "Did each fetch return a catalog of about the right size.",
               ("week",), lambda p: [Step("fetch-health", ["fetch-health", "--run-date", p["week"], "--force"])]),
        Action("merge", "Merge", "Union the week's connector partitions into the run the build reads.",
               ("week",), lambda p: [Step("merge", ["merge", "--run-date", p["week"]])]),
        Action("enrich-week", "Enrich", "Claude's enrichment candidates from the merged week (spends).",
               ("week",), lambda p: [Step("enrich-week", ["enrich-week", "--submit", "--run-date", p["week"]])]),
        Action("classify", "Classify", "Claude's coffee-or-not verdicts for new product versions (spends).", (),
               lambda p: [Step("build", _sh('rc=0; coffeedata classify || rc=1; '
                                            'coffeedata publish --message "classify (admin)"; exit $rc'))]),
        Action("build", "Build", "The weekly build: classify, build and gate. It serves the release if it passes.",
               (), lambda p: [Step("build", None)]),
        # From the Release page: a draft build over a decisions snapshot, Publish, Roll back.
        Action("draft", "Draft build", "Build the decisions as a draft: recorded, never served.", (),
               lambda p: [Step("build", _sh(
                   f"rc=0; coffeedata build --draft {p['draft_id']} --snapshot {p['snapshot_id']} "
                   f"--snapshot-sha256 {p['sha256']} || rc=1; "
                   f'coffeedata publish --message "draft {p["draft_id"]}"; exit $rc'))],
               page="release", acts_on_week=False),
        Action("publish", "Publish", "Serve a draft build: the publish job.", (),
               lambda p: [Step("publish", ["publish-release", "--build", p["build"], "--action", "publish",
                                           "--baseline", p["baseline"] or "none", "--reason", p["reason"]])],
               page="release", acts_on_week=False),
        Action("rollback", "Roll back", "Serve an earlier release again: the publish job.", (),
               lambda p: [Step("publish", ["publish-release", "--build", p["build"], "--action", "rollback",
                                           "--reason", p["reason"]])],
               page="release", acts_on_week=False),
    )
}  # fmt: skip
JOB_NAMES = sorted(
    {*CRAWL_JOB.values(), "crawl-retry", "fetch-health", "merge", "enrich-week", "build", "publish"}
)


def checked(action: Action, raw: dict, today: date, new_test_id: Callable[[], str]) -> dict:
    """The action's fields from a form, checked (ValueError says which is wrong). Adds the week (its Monday) and,
    for test mode, a new test id."""
    p: dict = {}
    for name in action.fields:
        f, v = FIELDS[name], str(raw.get(name) or FIELDS[name].default).strip()
        if f.choices and v not in f.choices:
            raise ValueError(f"{f.label}: choose one of {', '.join(f.choices)}")
        if f.check and v and not f.check.fullmatch(v):
            raise ValueError(f"{f.label}: {v!r} isn't valid")
        p[name] = v
    week = p.get("week") or today.isoformat()
    try:
        p["week"] = week_of(week)
    except ValueError as e:
        raise ValueError("Week: not a date") from e
    if "roaster" in action.fields and not p.get("roaster"):
        raise ValueError("Roaster: required")
    if p.get("mode") == "test":
        p["test_id"] = new_test_id()
        if not TEST_ID.fullmatch(p["test_id"]):
            raise ValueError("bad test id")
    return p


@dataclass
class WeekState:
    """What the lake says about a week, for the guards."""

    week: str
    merge_done: bool
    build_ended: bool = False


@dataclass
class Plan:
    action: Action
    params: dict
    steps: list[Step]
    refusals: list[str] = field(default_factory=list)
    window: str | None = None  # the scheduled window it falls in, if any (overridable)


def in_window(now: datetime, current: WeekState) -> str | None:
    """The scheduled window `now` is in, if any: Monday's crawls (09:00 UTC until the week's merge is done) or
    Tuesday's release (03:00-12:00 UTC: merge, enrich, build, the site)."""
    now = now.astimezone(UTC)
    monday = datetime.combine(date.fromisoformat(current.week), time(9, 0), UTC)
    if monday <= now and not current.merge_done and now < monday + timedelta(days=1, hours=3):
        return "the Monday crawl window (09:00 UTC until the week's merge is done)"
    tuesday = monday + timedelta(days=1)
    if tuesday.replace(hour=3) <= now < tuesday.replace(hour=12):
        return "the Tuesday release window (03:00-12:00 UTC: merge, enrich, build and the site)"
    return None


def guard(plan: Plan, now: datetime, current: WeekState, target: WeekState, running: dict[str, str],
          recent_test: dict | None) -> Plan:  # fmt: skip
    """The guards' refusals for a plan (empty: it may start). `running`: job -> its running execution; `target`:
    the week the action names; `recent_test`: the roaster's last test crawl in the last day."""
    out = plan.refusals
    for s in plan.steps:
        if s.job in running:
            out.append(f"{s.job} is already running ({running[s.job]}): wait for it, or cancel it first")
    if plan.params.get("mode") == "count" and target.merge_done:
        out.append(f"week {target.week} is merged: a merged week's partition isn't crawled again "
                   "(run a test crawl, or the merge again and a draft build)")  # fmt: skip
    if plan.action.key == "crawl-one" and plan.params.get("mode") == "test" and recent_test:
        out.append(f"{plan.params['roaster']} had a test crawl in the last day ({recent_test['run_id']})")
    plan.window = in_window(now, current)
    return plan
