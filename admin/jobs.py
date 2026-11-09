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
ROASTERS = re.compile(rf"{ROASTER.pattern}(,{ROASTER.pattern}){{0,19}}")  # 1 to 20 domains, comma-separated
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
    "roaster": Field("Roaster (its domain)", check=ROASTER, help="onyxcoffeelab.com"),
    "mode": Field(
        "Mode",
        ("test", "count"),
        default="test",
        help="Test: a trial run that never counts. Count: counts as this week's crawl.",
    ),  # fmt: skip
    "week": Field("Week (any day of it)", check=re.compile(r"\d{4}-\d{2}-\d{2}"), help="Only for Count"),
    "roasters": Field("Places (domains)", check=ROASTERS),
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
    page: str = "jobs"  # jobs: on the Jobs page; release: from the Release page; sources: from Sources


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
        Action("crawl", "Crawl", "Every store on one platform, or on all five. Use it to redo a whole crawl.",
               ("connectors", "mode", "week"), _crawl),
        Action("crawl-one", "Crawl one roaster", "One store, even if it's done or skipped this week. In Test mode it's the store's live check.", ("connector", "roaster", "mode", "week"),
               lambda p: [Step(CRAWL_JOB[p["connector"]],
                               ["crawl", "--connector", p["connector"], "--only", p["roaster"], *_mode(p)])]),
        Action("crawl-retry", "Shopify retry", "Try again the Shopify stores that blocked us, from where they stopped.",
               ("mode", "week"),
               lambda p: [Step("crawl-retry", ["crawl-retry", "--connector", "shopify", *_mode(p)])]),
        Action("fetch-health", "Fetch health", "Check that each store's crawl brought back about as many products as usual.",
               ("week",), lambda p: [Step("fetch-health", ["fetch-health", "--run-date", p["week"], "--force"])]),
        Action("merge", "Merge", "Combine this week's crawls so the build can use them.",
               ("week",), lambda p: [Step("merge", ["merge", "--run-date", p["week"]])]),
        Action("enrich-week", "Enrich", "Ask Claude to read this week's new product descriptions. It costs money.",
               ("week",), lambda p: [Step("enrich-week", ["enrich-week", "--submit", "--run-date", p["week"]])]),
        Action("classify", "Classify", "Ask Claude which new products are coffee. It costs money.", (),
               lambda p: [Step("build", _sh('rc=0; coffeedata classify || rc=1; '
                                            'coffeedata publish --message "classify (admin)"; exit $rc'))]),
        Action("build", "Build", "Build the catalog and check it. If every check passes, it goes live.",
               (), lambda p: [Step("build", None)]),
        # From the Release page: Claude's verdict on exactly the products a draft build's census lists as taken out by a
        # new keyword filter with no verdict (Classify these); the build id is checked before it reaches the script.
        Action("classify-review", "Classify for a review", "Ask Claude about the products a draft's keyword filters "
               "take out that it hasn't seen. It costs money.", (),
               lambda p: [Step("build", _sh(
                   f"rc=0; coffeedata classify --prefilter-build {p['build']} || rc=1; "
                   f'coffeedata publish --message "classify for draft {p["draft_id"]}"; exit $rc'))],
               page="release"),
        # From the Release page: a draft build over a decisions snapshot, Publish, Roll back.
        Action("draft", "Draft build", "Build your decisions as a draft to review. It doesn't go live.", (),
               lambda p: [Step("build", _sh(
                   f"rc=0; coffeedata build --draft {p['draft_id']} --snapshot {p['snapshot_id']} "
                   f"--snapshot-sha256 {p['sha256']} || rc=1; "
                   f'coffeedata publish --message "draft {p["draft_id"]}"; exit $rc'))],
               page="release"),
        # From the Sources page: Pull now, for places the owner flagged Roaster. One job: the probe, then a counted
        # crawl of each storefront found on its connector, into `week` (Writes.plan: the next week once this
        # week's merge is done, so the next merge and build keep it), then publish the probes.
        Action("pull", "Pull now", "Find each place's shop, then crawl it for this week. No Publish needed.",
               ("roasters",), lambda p: [Step("pull", _sh(
                   f'rc=0; coffeedata pull --only {p["roasters"]} --run-date {p["week"]} || rc=1; '
                   f'coffeedata publish --message "pull (admin)"; exit $rc'))],
               page="sources"),
        Action("publish", "Publish", "Make a draft build live.", (),
               lambda p: [Step("publish", ["publish-release", "--build", p["build"], "--action", "publish",
                                           "--baseline", p["baseline"] or "none", f"--reason={p['reason']}"])],
               page="release"),
        Action("rollback", "Roll back", "Make an earlier release live again.", (),
               lambda p: [Step("publish", ["publish-release", "--build", p["build"], "--action", "rollback",
                                           f"--reason={p['reason']}"])],
               page="release"),
    )
}  # fmt: skip
# Every job an action can start, from the table itself (each action's steps for a sample of every field): the list
# admin_control.tf grants, checked by tests/test_admin_terraform.py. Adding a job is one Action above.
_SAMPLE = {"connectors": "all", "connector": "shopify", "roaster": "a.example", "mode": "test", "week": "2026-01-05",
           "test_id": "t", "draft_id": "d", "snapshot_id": "s", "sha256": "0" * 64, "build": "b", "baseline": "b",
           "reason": "r", "roasters": "a.example"}  # fmt: skip
JOB_NAMES = sorted({s.job for a in ACTIONS.values() for s in a.steps(_SAMPLE)})


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
    for name in ("roaster", "roasters"):
        if name in action.fields and not p.get(name):
            raise ValueError(f"{FIELDS[name].label}: required")
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
