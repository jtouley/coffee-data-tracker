"""Triage issues for failed eval floors and production-health breaches: one GitHub issue per alert key, updated in
place, closed when the alert recovers (docs/TRIAGE.md).

    python -m coffeedata.triage evals <report.json> --workflow <name> [--dry-run]
    python -m coffeedata.triage prod <alerts.json> [--dry-run]

An alert has a key (`eval:origin`, `prod:grams`, `pipeline:eval-floors`). The issue for a key is found by the marker
`<!-- triage-key: <key> -->` in its body, among the issues labelled `triage` (open or closed) that the workflows'
bot opened (TRIAGE_AUTHOR, default github-actions[bot]), never by a search (GitHub's search index lags, and two runs
close together would open two issues). The bot rewrites the body on every run: notes go in comments. Then:
- firing, no issue: open one, labelled `triage`, the kind's label and the severity;
- firing, an open issue: rewrite its body (the latest run), and comment only when what fails changed (a fingerprint
  of the failing checks is kept in the body), so a weekly run doesn't add a comment a week;
- firing, a closed issue: reopen it with a comment, so one key keeps one history;
- recovered, an open issue: comment and close it, but only when every check the issue was opened or last updated
  for (kept in the body) was measured again and passed. A key, or a failing check, the run didn't measure (a
  skipped floor, a metric not in this release) leaves the issue as it is.

It needs only GITHUB_TOKEN with `issues: write` and GITHUB_REPOSITORY; it reads nothing else and runs no tested code.
`--dry-run` prints what it would do and calls nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

TRIAGE = "triage"
AUTHOR = "github-actions[bot]"  # who opens triage issues: GITHUB_TOKEN in the alert jobs
KIND_LABEL = {"eval": "eval-regression", "pipeline": "eval-regression", "prod": "prod-health"}
SEVERITIES = ("critical", "warning")
OK = ("ok", "expected")  # a check that isn't failing: passed, or a breach an acknowledged change explains
LABELS = {
    TRIAGE: ("d93f0b", "Opened by an alert: triage with docs/TRIAGE.md"),
    "eval-regression": ("b60205", "An accuracy floor or ceiling failed on main or in eval-floors"),
    "prod-health": ("fbca04", "A production build's health metric breached its threshold"),
    "severity:critical": ("b60205", "Act this week: the data customers get is likely wrong"),
    "severity:warning": ("fef2c0", "Look before the next weekly run"),
}
RUNBOOK = "docs/TRIAGE.md"
# The first steps of each kind of alert; docs/TRIAGE.md has the rest.
FIRST_STEPS = {
    "eval": [
        "Open the run below and read the failing check; the measured value, the bound and the set are in the table.",
        "List what changed since the last green run of the same workflow: `git log <last green sha>..<this sha>` "
        "(code, `uv.lock`, the labels under `eval/`).",
        "Reproduce: `uv run pytest -q {source}` (a private floor needs its inputs: `eval/floor_inputs.py`).",
        "A change that lowered the score: revert it or fix it. Never lower a floor or raise a ceiling to pass "
        "(CLAUDE.md, step 8).",
    ],
    "pipeline": [
        "The floors didn't run, so nothing was measured: open the run and read the first failing step.",
        "A token, download or checksum failure: `eval/floor_inputs.py` (docs/RULES.md, Private inputs).",
        "Re-run the workflow by hand once fixed; this issue closes on the next run that scores the floors.",
    ],
    "prod": [
        "Open the run below; the table names each metric, its status and the bound it crossed. For the values, "
        "re-run the check locally on a read-only copy of the lake (docs/TRIAGE.md, step 4).",
        "Was it intended? Read the PRs merged since the previous release (their Effect sections) and the CHANGELOG. "
        "A change that explains it gets an entry in config/prod_health_expected.toml, not a rollback.",
        "Compare the served release with the previous one: `uv run coffeedata build-diff` (RUNBOOK.md §5.1 a), "
        "and check the week's crawl (fetch-health, job_manifests).",
        "Customers get bad data that no change explains (a critical fill, price or volume breach that build-diff "
        "confirms): roll back with `gh workflow run weekly -f rollback_to=previous` (docs/TRIAGE.md, When to roll "
        "back). The pipeline itself is never blocked by this alert.",
    ],
}


class Client(Protocol):
    def issues(self) -> list[dict[str, Any]]: ...
    def create(self, title: str, body: str, labels: list[str]) -> dict[str, Any]: ...
    def update(self, number: int, **fields: Any) -> None: ...
    def comment(self, number: int, body: str) -> None: ...
    def add_labels(self, number: int, labels: list[str]) -> None: ...
    def remove_label(self, number: int, label: str) -> None: ...
    def ensure_label(self, name: str, color: str, description: str) -> None: ...


class GitHub:
    """The few REST calls triage needs, with the workflow's GITHUB_TOKEN."""

    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.base, self.token = f"{api.rstrip('/')}/repos/{repo}", token

    def _call(self, method: str, path: str, body: dict | None = None, attempts: int = 4) -> Any:
        """One REST call; a network error, 429 or 5xx is retried with backoff (2, 4, 8 s)."""
        for attempt in range(1, attempts + 1):
            try:
                return self._once(method, path, body)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == attempts:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == attempts:
                    raise
            time.sleep(2.0**attempt)
        raise AssertionError("unreachable")

    def _once(self, method: str, path: str, body: dict | None) -> Any:
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "coffeedata-triage",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        return json.loads(data) if data else None

    def issues(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 11):  # newest first; 1,000 triage issues is far more than this repo will have
            got = self._call(
                "GET", f"/issues?labels={TRIAGE}&state=all&per_page=100&page={page}&sort=created"
            )
            out += [i for i in got if "pull_request" not in i]
            if len(got) < 100:
                break
        return out

    def create(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._call("POST", "/issues", {"title": title, "body": body, "labels": labels})

    def update(self, number: int, **fields: Any) -> None:
        self._call("PATCH", f"/issues/{number}", fields)

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/issues/{number}/comments", {"body": body})

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._call("POST", f"/issues/{number}/labels", {"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._call("DELETE", f"/issues/{number}/labels/{label}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def ensure_label(self, name: str, color: str, description: str) -> None:
        try:
            self._call("POST", "/labels", {"name": name, "color": color, "description": description})
        except urllib.error.HTTPError as e:
            if e.code != 422:  # 422: it exists already
                raise


class DryRun:
    """Prints each call instead of making it; finds no existing issue."""

    def __init__(self) -> None:
        self.n = 0

    def issues(self) -> list[dict[str, Any]]:
        return []

    def create(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        self.n += 1
        print(f"--- would open issue {title!r} labels={labels}\n{body}\n---")
        return {"number": -self.n}

    def update(self, number: int, **fields: Any) -> None:
        print(f"would update #{number}: {sorted(fields)}")

    def comment(self, number: int, body: str) -> None:
        print(f"would comment on #{number}: {body.splitlines()[0]}")

    def add_labels(self, number: int, labels: list[str]) -> None:
        print(f"would label #{number}: {labels}")

    def remove_label(self, number: int, label: str) -> None:
        print(f"would unlabel #{number}: {label}")

    def ensure_label(self, name: str, color: str, description: str) -> None:
        pass


# --- alerts --------------------------------------------------------------------------------------------------


def _marker(name: str, value: str) -> str:
    return f"<!-- triage-{name}: {value} -->"


def _read_marker(body: str | None, name: str) -> str | None:
    prefix = f"<!-- triage-{name}: "
    for line in (body or "").splitlines():
        if line.startswith(prefix) and line.endswith(" -->"):
            return line[len(prefix) : -4]
    return None


def _failing_names(alert: dict[str, Any]) -> list[str]:
    return sorted(f"{c.get('set') or ''}|{c['name']}" for c in alert["checks"] if c["status"] not in OK)


def _passed_names(alert: dict[str, Any]) -> set[str]:
    return {f"{c.get('set') or ''}|{c['name']}" for c in alert["checks"] if c["status"] in OK}


def fingerprint(alert: dict[str, Any]) -> str:
    """What fails, not by how much: a new comment when a check starts or stops failing or changes severity."""
    failing = sorted(
        f"{c.get('set') or ''}|{c['name']}|{c['status']}" for c in alert["checks"] if c["status"] not in OK
    )
    return hashlib.sha256(json.dumps([alert["severity"], failing]).encode()).hexdigest()[:16]


def _fmt(x: Any, unit: str | None = None) -> str:
    if x is None:
        return "–"
    if isinstance(x, str):
        return x
    if unit == "count" or (unit is None and float(x).is_integer() and abs(x) > 1):
        return f"{x:,.0f}"
    if unit in ("share", "percent") or (unit is None and 0 <= x <= 1):
        return f"{x:.2%}"
    return f"{x:.4g}"


def _row(c: dict[str, Any], values: bool = True) -> str:
    unit = c.get("unit")
    bound = "–"
    if c.get("threshold") is not None:
        word = {">=": "floor", "<=": "ceiling", "<": "below", ">": "above"}.get(c.get("op") or "", "")
        bound = f"{c.get('kind') or word} {_fmt(c['threshold'], unit)}"
    ci = c.get("interval")
    ci_text = f"[{ci[0]:.1%}, {ci[1]:.1%}]" if ci else "–"
    base = _fmt(c.get("baseline"), unit) if "baseline" in c else None
    status = c["status"] + (f" (#{c['pr']})" if c.get("pr") else "")
    if (
        not values
    ):  # a production metric: the lake is private, so the issue names the check and its bound only
        return "| " + " | ".join([c.get("set") or "", f"`{c['name']}`", status, bound]) + " |"
    cells = [c.get("set") or "", f"`{c['name']}`", status, _fmt(c.get("value"), unit), bound]
    cells += [base or "–"] if "baseline" in c else [ci_text]
    return "| " + " | ".join(cells) + " |"


def render(alert: dict[str, Any], now: str) -> str:
    kind = alert["kind"]
    prod = kind == "prod"
    head = (
        ["Platform", "Check", "Status", "Bound"]
        if prod
        else ["Set", "Check", "Status", "Measured", "Bound", "Wilson 95%"]
    )
    lines = [
        _marker("key", alert["key"]),
        _marker("fingerprint", fingerprint(alert)),
        _marker("failing", json.dumps(_failing_names(alert))),
        f"**{alert['summary']}**" if alert.get("summary") else "",
        "",
        f"Severity: **{alert['severity']}**. Source: {alert.get('source') or '–'}.",
        "",
        "| " + " | ".join(head) + " |",
        "|" + "---|" * len(head),
        *(_row(c, values=not prod) for c in alert["checks"]),
        "",
    ]
    for m in alert.get("messages") or []:
        lines += ["```", m.strip()[:1500], "```", ""]
    ctx = alert.get("context") or {}
    for k, v in ctx.items():
        lines.append(f"- {k}: {v if v else '–'}")
    lines += ["", f"### First steps ({RUNBOOK})", ""]
    steps = alert.get("steps") or FIRST_STEPS[kind]
    lines += [f"{i}. {s.format(source=alert.get('source') or '<the test>')}" for i, s in enumerate(steps, 1)]
    lines += [
        "",
        f"_Updated {now} by the alert job, which rewrites this description on every run (add notes as comments). "
        "It closes this issue once every failing check is measured again and passes._",
    ]
    return "\n".join(lines)


def sync(
    alerts: list[dict[str, Any]], gh: Client, now: str | None = None, author: str = AUTHOR
) -> list[tuple[str, str, int]]:
    """Open, update, reopen or close one issue per alert key. Returns (action, key, issue number)."""
    now = now or datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if any(a["firing"] for a in alerts):
        for name, (color, desc) in LABELS.items():
            gh.ensure_label(name, color, desc)
    existing: dict[str, dict[str, Any]] = {}
    for found in gh.issues():  # newest first: the first issue seen for a key is the one kept
        if (found.get("user") or {}).get("login") != author:
            continue  # a hand-made or copied issue never takes over a key
        key = _read_marker(found.get("body"), "key")
        if key and key not in existing:
            existing[key] = found
    done: list[tuple[str, str, int]] = []
    for a in alerts:
        issue = existing.get(a["key"])
        run = (a.get("context") or {}).get("run") or "this run"
        if not a["firing"]:
            if issue and issue["state"] == "open":
                was = json.loads(_read_marker(issue.get("body"), "failing") or "[]")
                if not set(was) <= _passed_names(a):
                    continue  # a check that failed wasn't measured this time: nothing is known to have recovered
                gh.comment(issue["number"], f"Recovered: every check passed in {run}. Closing.")
                gh.update(issue["number"], state="closed", state_reason="completed")
                done.append(("closed", a["key"], issue["number"]))
            continue
        body = render(a, now)
        sev = f"severity:{a['severity']}"
        labels = [TRIAGE, a.get("label") or KIND_LABEL[a["kind"]], sev]
        title = f"[triage] {a['title']}"
        if issue is None:
            new = gh.create(title, body, labels)
            done.append(("opened", a["key"], new["number"]))
            continue
        n = issue["number"]
        changed = _read_marker(issue.get("body"), "fingerprint") != fingerprint(a)
        if issue["state"] != "open":
            gh.update(n, state="open", title=title, body=body)
            gh.comment(n, f"Firing again in {run}: {_failing(a)}")
            done.append(("reopened", a["key"], n))
        else:
            gh.update(n, title=title, body=body)
            if changed:
                gh.comment(n, f"What fails changed in {run}: {_failing(a)}")
            done.append(("updated", a["key"], n))
        gh.add_labels(n, labels)
        for other in SEVERITIES:
            if f"severity:{other}" != sev and any(
                lb.get("name") == f"severity:{other}" for lb in issue.get("labels", [])
            ):
                gh.remove_label(n, f"severity:{other}")
    return done


def _failing(a: dict[str, Any]) -> str:
    bad = [c for c in a["checks"] if c["status"] not in OK]
    values = a["kind"] != "prod"
    return "; ".join(
        f"{c.get('set') or ''} {c['name']} {_fmt(c.get('value'), c.get('unit')) if values else c['status']}".strip()
        for c in bad
    )


# --- the eval report -----------------------------------------------------------------------------------------


def eval_alerts(
    report: dict[str, Any] | None,
    workflow: str,
    run_url: str | None,
    sha: str | None,
    alert_if_missing: bool = True,
    scope: str | None = None,
) -> list[dict]:
    """One alert per eval in the floors' report (tests/evalfloor.py) that `scope` owns: "in-git" (ci: every floor
    but the private ones) or "private" (eval-floors: the private_eval floors). A workflow never judges an eval it
    doesn't own, so ci can't close an issue eval-floors opened. With `alert_if_missing`, one more alert for the run
    itself: firing unless the report scored at least one floor in scope (a missing or empty report scored none).
    ci leaves that one out: a red main whose tests never reached the floors (a lint failure) is plain to see."""
    ctx = {"workflow": workflow, "commit": sha, "run": run_url}
    tests = [
        t for t in (report or {}).get("tests", [])
        if t.get("eval") and (scope is None or bool(t.get("private")) == (scope == "private"))
    ]  # fmt: skip
    scored = any(t["outcome"] != "skipped" for t in tests)
    out = []
    if alert_if_missing:
        out.append({
            "key": f"pipeline:{workflow}", "kind": "pipeline", "title": f"{workflow}: the floors didn't run",
            "severity": "warning", "context": ctx, "source": f".github/workflows/{workflow}.yml",
            "summary": f"The {workflow} run scored no floor"
            + (" (no report)." if report is None else " (the report holds none)."),
            "firing": not scored,
            "checks": [{"name": "floors scored", "status": "ok" if scored else "warning"}],
        })  # fmt: skip
    by_eval: dict[str, list[dict]] = {}
    for t in tests:
        by_eval.setdefault(t["eval"], []).append(t)
    for name, ts in sorted(by_eval.items()):
        measured = [t for t in ts if t["outcome"] != "skipped"]
        if not measured:
            continue  # skipped everywhere: nothing was measured, so nothing changes
        bad = [t for t in measured if t["outcome"] in ("failed", "error")]
        checks: list[dict[str, Any]] = []
        messages = []
        for t in bad or measured:
            status = "error" if t["outcome"] == "error" else "failed"
            got = [c for c in t["checks"] if not (bad and c.get("ok"))]
            if bad and not got:  # it failed without a failing check (it crashed): say so, with its message
                got = [{"set": t["set"], "metric": t["nodeid"].split("::")[-1].split("[")[0], "ok": False}]
                messages.append(t["message"] or "(no message)")
            for c in got:
                checks.append({
                    "set": c.get("set") or t["set"], "name": c["metric"],
                    "status": "ok" if c.get("ok") else status,
                    "value": c.get("value"), "threshold": c.get("bound"), "op": c.get("op"), "kind": c.get("kind"),
                    "unit": c.get("unit"), "interval": c.get("interval"),
                })  # fmt: skip
        source = ts[0]["nodeid"].split("::")[0]
        sets = sorted({c["set"] for c in checks if c["status"] not in OK})
        out.append({
            "key": f"eval:{name}", "kind": "eval", "title": f"eval regression: {name}", "severity": "critical",
            "firing": bool(bad), "checks": checks, "source": source, "context": ctx,
            "summary": f"The {name} eval failed on {', '.join(sets)} in {workflow}." if bad else "",
            "messages": messages,
        })  # fmt: skip
    return out


def prod_pipeline_alert(measured: bool, run_url: str | None, sha: str | None) -> list[dict]:
    """The health check itself: firing when it produced no alerts file (the lake unreadable, a schema break)."""
    return [{
        "key": "pipeline:prod-health", "kind": "pipeline", "label": "prod-health",
        "title": "prod-health: the check didn't run",
        "severity": "warning", "firing": not measured, "source": ".github/workflows/prod-health.yml",
        "summary": "The production health check couldn't measure the served release, so nothing was judged.",
        "checks": [{"name": "served release measured", "status": "ok" if measured else "warning"}],
        "context": {"commit": sha, "run": run_url},
        "steps": [
            "Open the run and read the first failing step (fetch: the Hub token or the lake; measure: a release "
            "file or a column missing).",
            "Nothing is served yet, or the served release has no dim_coffee: see docs/ingestion/RUNBOOK.md §5.",
            "Re-run the workflow by hand once fixed; this issue closes on the next run that measures.",
        ],
    }]  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m coffeedata.triage", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evals", help="issues from the floors' report (EVAL_REPORT)")
    e.add_argument("report", type=Path, help="the report; a missing file means the floors never ran")
    e.add_argument("--workflow", required=True)
    e.add_argument(
        "--alert-if-missing",
        action="store_true",
        help="a report that scored no floor opens pipeline:<workflow>",
    )
    e.add_argument("--scope", choices=("in-git", "private"), help="the evals this workflow owns")
    pr = sub.add_parser("prod", help="issues from coffeedata.prod_health judge --out")
    pr.add_argument("alerts", type=Path)
    for s in (e, pr):
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--run-url", help="the run the alert is about (default: this run)")
        s.add_argument("--sha", help="the commit it ran on (default: GITHUB_SHA)")
    a = p.parse_args(argv)
    server, repo, run_id = (
        os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    run_url = a.run_url or (f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None)
    sha = a.sha or os.environ.get("GITHUB_SHA")
    if a.cmd == "evals":
        report = json.loads(a.report.read_text()) if a.report.exists() else None
        alerts = eval_alerts(report, a.workflow, run_url, sha, a.alert_if_missing, a.scope)
    else:
        alerts = prod_pipeline_alert(a.alerts.exists(), run_url, sha)
        alerts += json.loads(a.alerts.read_text()) if a.alerts.exists() else []
    token = os.environ.get("GITHUB_TOKEN")
    gh: Client
    if a.dry_run:
        gh = DryRun()
    elif not (token and repo):
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are needed (or --dry-run)", file=sys.stderr)
        return 2
    else:
        gh = GitHub(repo, token)
    for action, key, number in sync(alerts, gh, author=os.environ.get("TRIAGE_AUTHOR") or AUTHOR):
        print(f"{action} {key} #{number}")
    firing = [x["key"] for x in alerts if x["firing"]]
    print(f"{len(firing)} alert(s) firing: {', '.join(firing) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
