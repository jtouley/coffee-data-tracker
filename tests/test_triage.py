"""The eval alerts (docs/TRIAGE.md): the floors' failure messages and report (tests/evalfloor.py), and the triage issues
coffeedata.triage keeps, offline, against an in-memory GitHub: one issue per eval, updated in place, reopened rather
than duplicated, closed on recovery. No real issue is created."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coffeedata import triage

from .evalfloor import _from_message, floors, wilson

ROOT = Path(__file__).parent.parent


class FakeGitHub:
    """The calls triage makes, kept in memory, newest issue first as the REST listing returns them."""

    def __init__(self):
        self.store: list[dict] = []
        self.comments: dict[int, list[str]] = {}
        self.labels_made: set[str] = set()
        self.calls: list[str] = []

    def issues(self):
        return [dict(i, labels=[{"name": n} for n in i["labels"]]) for i in reversed(self.store)]

    def _get(self, n):
        return next(i for i in self.store if i["number"] == n)

    def create(self, title, body, labels):
        n = len(self.store) + 1
        self.store.append(
            {"number": n, "title": title, "body": body, "labels": list(labels), "state": "open"}
        )
        self.calls.append(f"create {n}")
        return {"number": n}

    def update(self, number, **fields):
        fields.pop("state_reason", None)
        self._get(number).update(fields)
        self.calls.append(f"update {number} {sorted(fields)}")

    def comment(self, number, body):
        self.comments.setdefault(number, []).append(body)

    def add_labels(self, number, labels):
        i = self._get(number)
        i["labels"] += [lb for lb in labels if lb not in i["labels"]]

    def remove_label(self, number, label):
        self._get(number)["labels"].remove(label)

    def ensure_label(self, name, color, description):
        self.labels_made.add(name)


def _test(eval_name, set_name, outcome, value=None, bound=None, kind="floor", message="", interval=None):
    checks = []
    if value is not None:
        ok = outcome == "passed"
        checks = [{"eval": eval_name, "set": set_name, "metric": "weighted agreement", "value": value,
                   "bound": bound, "kind": kind, "op": ">=" if kind == "floor" else "<=", "ok": ok,
                   "unit": "share", "n": None, "interval": interval}]  # fmt: skip
    return {"nodeid": f"tests/test_{eval_name}_accuracy.py::test_x[{set_name}]", "eval": eval_name,
            "set": set_name, "outcome": outcome, "checks": checks, "message": message}  # fmt: skip


def _report(*tests):
    return {"version": 1, "commit": "abc", "tests": list(tests)}


def _sync(gh, report, run="https://github.com/o/r/actions/runs/1"):
    return triage.sync(triage.eval_alerts(report, "eval-floors", run, "abc123"), gh, now="2026-11-11")


def test_a_failed_floor_names_the_eval_set_value_and_floor():
    with pytest.raises(AssertionError) as e, floors("origin", "train") as f:
        f.at_least("weighted agreement", 0.951, 0.959)
        f.at_most("weighted wrong-country", 0.03, 0.040)
    assert str(e.value) == "eval origin, set train: weighted agreement 95.10% < floor 95.90%"


def test_a_count_floor_carries_its_wilson_interval_and_every_miss():
    with pytest.raises(AssertionError) as e, floors("match", "validation") as f:
        f.at_least("right", 1600, 1632, unit="count", n=2000)
        f.at_most("confidently wrong", 5, 3, unit="count", n=2000)
    lo, hi = wilson(1600, 2000)
    lines = str(e.value).splitlines()
    assert lines[0] == (
        f"eval match, set validation: right 1,600 of 2,000 < floor 1,632 (Wilson 95% {lo:.1%}-{hi:.1%})"
    )
    assert lines[1].startswith("eval match, set validation: confidently wrong 5 of 2,000 > ceiling 3")


def test_a_passing_block_raises_nothing():
    with floors("sealed", "rules") as f:
        f.at_least("weighted accuracy", 0.95, 0.94)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("train: weighted accuracy 0.9900 < floor 0.999\n  a miss", ("train", "weighted accuracy", 0.99, 0.999, "floor")),
        ("validation: weighted wrong 0.0200 > ceiling 0.011", ("validation", "weighted wrong", 0.02, 0.011, "ceiling")),
        ("full-population accuracy 0.9412 < 0.95 (eval/bag_grams/README.md)",
         (None, "full-population accuracy", 0.9412, 0.95, "floor")),
        ("train: 98.0% < 98.5% (12 wrong)", ("train", "", 0.98, 0.985, "floor")),
    ],
)  # fmt: skip
def test_a_plain_floor_assertion_is_read_from_its_message(message, expected):
    c = _from_message("AssertionError: " + message)
    assert c is not None
    assert (c["set"], c["metric"], round(c["value"], 6), c["bound"], c["kind"]) == expected


def test_an_unreadable_message_is_kept_as_it_is():
    assert _from_message("AssertionError: the redrawn sample isn't the labelled one") is None
    alerts = triage.eval_alerts(
        _report(_test("dq", "price/train", "error", message="the redrawn sample isn't the labelled one")),
        "eval-floors", None, None,
    )  # fmt: skip
    dq = next(a for a in alerts if a["key"] == "eval:dq")
    assert dq["firing"] and dq["messages"] == ["the redrawn sample isn't the labelled one"]
    assert dq["checks"][0]["status"] == "error"


def test_one_issue_per_eval_updated_reopened_and_closed():
    gh = FakeGitHub()
    failing = _report(
        _test("origin", "train", "failed", 0.95, 0.959), _test("origin", "validation", "passed", 0.9, 0.8)
    )
    assert _sync(gh, failing) == [("opened", "eval:origin", 1)]
    issue = gh.store[0]
    assert issue["title"] == "[triage] eval regression: origin"
    assert set(issue["labels"]) == {"triage", "eval-regression", "severity:critical"}
    assert {"triage", "eval-regression", "prod-health"} <= gh.labels_made
    body = issue["body"]
    assert "<!-- triage-key: eval:origin -->" in body
    assert "| train | `weighted agreement` | failed | 95.00% | floor 95.90% |" in body
    assert "abc123" in body and "https://github.com/o/r/actions/runs/1" in body
    assert "### First steps" in body and "uv run pytest -q tests/test_origin_accuracy.py" in body
    assert "validation" not in body.split("| Set |")[1].split("###")[0]  # only the failing set is listed

    # The same failure next week: the body is rewritten, nothing new is opened and no comment is added.
    assert _sync(gh, failing, run="https://github.com/o/r/actions/runs/2") == [("updated", "eval:origin", 1)]
    assert len(gh.store) == 1 and gh.comments.get(1) is None
    assert "runs/2" in gh.store[0]["body"]

    # What fails changes: one comment says so.
    worse = _report(
        _test("origin", "train", "failed", 0.95, 0.959), _test("origin", "validation", "failed", 0.7, 0.8)
    )
    _sync(gh, worse)
    assert len(gh.comments[1]) == 1 and "validation" in gh.comments[1][0]

    # Recovery: a comment and the issue closes.
    healthy = _report(
        _test("origin", "train", "passed", 0.96, 0.959), _test("origin", "validation", "passed", 0.9, 0.8)
    )
    assert _sync(gh, healthy) == [("closed", "eval:origin", 1)]
    assert gh.store[0]["state"] == "closed" and gh.comments[1][-1].startswith("Recovered")
    assert _sync(gh, healthy) == []  # nothing left to close

    # Firing again reopens the same issue instead of opening a new one.
    assert _sync(gh, failing) == [("reopened", "eval:origin", 1)]
    assert len(gh.store) == 1 and gh.store[0]["state"] == "open"
    assert gh.comments[1][-1].startswith("Firing again")


def test_a_skipped_eval_leaves_its_issue_alone():
    gh = FakeGitHub()
    _sync(gh, _report(_test("wholesale", "train", "failed", 0.9, 0.97)))
    assert _sync(gh, _report(_test("wholesale", "train", "skipped"))) == []
    assert gh.store[0]["state"] == "open"


def test_each_eval_has_its_own_issue_and_others_are_untouched():
    gh = FakeGitHub()
    actions = _sync(
        gh,
        _report(_test("origin", "train", "failed", 0.9, 0.959), _test("match", "train", "failed", 0.8, 0.81)),
    )
    assert sorted(a[:2] for a in actions) == [("opened", "eval:match"), ("opened", "eval:origin")]
    actions = _sync(
        gh,
        _report(
            _test("origin", "train", "passed", 0.96, 0.959), _test("match", "train", "failed", 0.8, 0.81)
        ),
    )
    assert sorted(a[:2] for a in actions) == [("closed", "eval:origin"), ("updated", "eval:match")]


def test_an_issue_is_found_by_its_key_not_its_title():
    gh = FakeGitHub()
    _sync(gh, _report(_test("origin", "train", "failed", 0.9, 0.959)))
    gh.store[0]["title"] = "origin floor broke (renamed by hand)"
    gh.create("an unrelated triage issue", "no marker here", ["triage"])
    assert _sync(gh, _report(_test("origin", "train", "failed", 0.9, 0.959))) == [
        ("updated", "eval:origin", 1)
    ]
    assert len(gh.store) == 2


def test_severity_labels_follow_the_alert():
    gh = FakeGitHub()
    alert = {"key": "prod:grams", "kind": "prod", "title": "prod health: grams", "firing": True,
             "severity": "warning", "checks": [{"name": "fill:grams", "status": "warning", "value": 0.7}]}  # fmt: skip
    triage.sync([alert], gh)
    triage.sync([alert | {"severity": "critical"}], gh)
    assert "severity:critical" in gh.store[0]["labels"] and "severity:warning" not in gh.store[0]["labels"]


def test_a_missing_report_opens_a_pipeline_issue_that_closes_once_floors_run():
    gh = FakeGitHub()
    assert _sync(gh, None) == [("opened", "pipeline:eval-floors", 1)]
    assert "floors didn't run" in gh.store[0]["title"]
    assert _sync(gh, _report(_test("origin", "train", "passed", 0.96, 0.959))) == [
        ("closed", "pipeline:eval-floors", 1)
    ]


def test_a_dry_run_calls_nothing(tmp_path, capsys):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report(_test("origin", "train", "failed", 0.9, 0.959))))
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "evals", str(report), "--workflow", "ci", "--dry-run"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "would open issue '[triage] eval regression: origin'" in r.stdout
    assert "1 alert(s) firing: eval:origin" in r.stdout


def test_without_a_token_it_refuses_rather_than_guessing(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "evals", str(tmp_path / "none.json"), "--workflow", "ci"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 2 and "GITHUB_TOKEN" in r.stderr


def test_the_report_and_annotations_from_a_real_session(tmp_path):
    """A pytest session, with two xdist workers, over a floor that uses `floors` and one that only asserts (as
    tests/test_bag_grams_accuracy.py does): the report names both, and each failure is an ::error annotation."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "conftest.py").write_text(
        "from tests_floor.evalfloor import (  # noqa: F401\n"
        "    pytest_runtest_call, pytest_runtest_logreport, pytest_sessionfinish, pytest_terminal_summary)\n"
    )
    pkg = tmp_path / "tests_floor"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "evalfloor.py").write_text((ROOT / "tests" / "evalfloor.py").read_text())
    (tests / "test_demo_accuracy.py").write_text(
        textwrap.dedent("""
        import pytest
        from tests_floor.evalfloor import floors

        @pytest.mark.parametrize("name", ["train", "validation"])
        def test_demo(name):
            with floors("demo", name) as f:
                f.at_least("accuracy", 0.9 if name == "train" else 0.99, 0.95, n=100, k=90 if name == "train" else 99)

        def test_full_population_gate():
            assert 0.9412 >= 0.95, "full-population accuracy 0.9412 < 0.95 (README)"

        def test_skipped():
            pytest.skip("no inputs")
        """)
    )
    report = tmp_path / "report.json"
    env = os.environ | {"EVAL_REPORT": str(report), "GITHUB_ACTIONS": "true", "PYTHONPATH": str(tmp_path)}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2", "--rootdir", str(tmp_path),
         "-c", os.devnull, "tests"],
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )  # fmt: skip
    assert r.returncode == 1, r.stdout + r.stderr
    doc = json.loads(report.read_text())
    by = {t["nodeid"].split("::")[1]: t for t in doc["tests"]}
    assert by["test_demo[validation]"]["outcome"] == "passed"
    assert by["test_demo[validation]"]["checks"][0]["interval"] == [round(x, 4) for x in wilson(99, 100)]
    train = by["test_demo[train]"]
    assert train["outcome"] == "failed" and train["eval"] == "demo" and train["set"] == "train"
    assert train["checks"][0]["ok"] is False and train["checks"][0]["bound"] == 0.95
    gate = by["test_full_population_gate"]
    assert gate["eval"] == "demo" and gate["set"] == "full_population_gate"
    assert gate["checks"][0]["metric"] == "full-population accuracy" and gate["checks"][0]["value"] == 0.9412
    assert by["test_skipped"]["outcome"] == "skipped"
    # the annotation escapes % as GitHub's workflow commands require
    assert (
        "::error title=Eval floor demo / train::eval demo, set train: accuracy 90.00%25 < floor 95.00%25"
        in r.stdout
    )
    assert "::error title=Eval floor demo / full_population_gate::" in r.stdout

    alerts = {a["key"]: a for a in triage.eval_alerts(doc, "ci", None, None)}
    assert alerts["eval:demo"]["firing"]
    assert {c["set"] for c in alerts["eval:demo"]["checks"]} == {"train", "full_population_gate"}


def test_ci_opens_no_pipeline_issue_for_a_missing_report():
    assert triage.eval_alerts(None, "ci", None, None, alert_if_missing=False) == []
    alerts = triage.eval_alerts(
        _report(_test("classifier", "train", "passed", 0.99, 0.985)), "ci", None, None, False
    )
    assert [a["key"] for a in alerts] == ["eval:classifier"]


def test_the_alert_jobs_hold_only_issues_write_and_never_a_hub_token():
    """Each workflow's alert job: main only, never on a pull request, GITHUB_TOKEN limited to issues, no secret;
    and the jobs that hold the Hub token can't write issues (docs/TRIAGE.md, Permissions and safety)."""
    import yaml

    workflows = ROOT / ".github" / "workflows"
    for name in ("ci.yml", "eval-floors.yml", "prod-health.yml"):
        wf = yaml.safe_load((workflows / name).read_text())
        assert wf["permissions"] == {"contents": "read"}, name
        for job_name, job in wf["jobs"].items():
            text = yaml.safe_dump(job)
            if job_name == "alert":
                assert job["permissions"] == {"contents": "read", "issues": "write"}, name
                assert "secrets." not in text, name
                assert "github.ref == 'refs/heads/main'" in job["if"], name
                if name == "ci.yml":
                    assert "github.event_name == 'push'" in job["if"]
            else:
                assert "issues" not in (job.get("permissions") or {}), (name, job_name)
            if "HF_TOKEN" in text:
                assert job.get("if") == "github.ref == 'refs/heads/main'", (name, job_name)
