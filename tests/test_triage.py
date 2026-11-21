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

    def by_hand(self, title, body):
        """An issue someone opened themselves (not the workflows' bot), labelled triage."""
        n = len(self.store) + 1
        self.store.append({"number": n, "title": title, "body": body, "labels": ["triage"], "state": "open",
                           "user": {"login": "someone"}})  # fmt: skip
        return n

    def _get(self, n):
        return next(i for i in self.store if i["number"] == n)

    def create(self, title, body, labels):
        n = len(self.store) + 1
        self.store.append(
            {
                "number": n,
                "title": title,
                "body": body,
                "labels": list(labels),
                "state": "open",
                "user": {"login": "github-actions[bot]"},
            }  # fmt: skip
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


def _test(eval_name, set_name, outcome, value=None, bound=None, kind="floor", message="", interval=None,
          private=False):  # fmt: skip
    checks = []
    if value is not None:
        ok = outcome == "passed"
        checks = [{"eval": eval_name, "set": set_name, "metric": "weighted agreement", "value": value,
                   "bound": bound, "kind": kind, "op": ">=" if kind == "floor" else "<=", "ok": ok,
                   "unit": "share", "n": None, "interval": interval}]  # fmt: skip
    return {"nodeid": f"tests/test_{eval_name}_accuracy.py::test_x[{set_name}]", "eval": eval_name,
            "set": set_name, "outcome": outcome, "checks": checks, "message": message,
            "private": private}  # fmt: skip


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

    # Only validation measured this time (train skipped): train's failure isn't known to be fixed, so it stays open.
    half = _report(_test("origin", "train", "skipped"), _test("origin", "validation", "passed", 0.9, 0.8))
    assert _sync(gh, half) == []
    assert gh.store[0]["state"] == "open"

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
    # nothing scored: the wholesale issue is left alone (the run itself is what's wrong: pipeline:eval-floors)
    assert _sync(gh, _report(_test("wholesale", "train", "skipped"))) == [
        ("opened", "pipeline:eval-floors", 2)
    ]
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
    # a newer copy of the bot's issue made by hand carries the same marker: it never takes the key over
    gh.by_hand("copy", gh.store[0]["body"])
    assert _sync(gh, _report(_test("origin", "train", "failed", 0.9, 0.959))) == [
        ("updated", "eval:origin", 1)
    ]
    assert len(gh.store) == 3


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
    assert "eval-regression" in gh.store[0]["labels"]
    # an empty report (a collection error) or one whose floors all skipped scored nothing: still firing
    assert _sync(gh, _report()) == [("updated", "pipeline:eval-floors", 1)]
    assert _sync(gh, _report(_test("origin", "train", "skipped"))) == [("updated", "pipeline:eval-floors", 1)]
    assert _sync(gh, _report(_test("origin", "train", "passed", 0.96, 0.959))) == [
        ("closed", "pipeline:eval-floors", 1)
    ]


def test_a_crashed_floor_opens_an_issue_that_says_what_crashed():
    """A floor that raised after recording only passing checks (or none): the issue names the test and its
    message, never an empty table."""
    crashed = _test("dq", "price/train", "failed", 0.9, 0.85, message="KeyError: 'price_strata.json'")
    crashed["checks"][0]["ok"] = True
    gh = FakeGitHub()
    _sync(gh, _report(crashed))
    body = gh.store[0]["body"]
    assert "The dq eval failed on price/train in eval-floors." in body
    assert "| price/train | `test_x` | failed |" in body and "KeyError: 'price_strata.json'" in body


def _floor(eval_name, nodeid_set, outcome, checks=(), message=""):
    """A report entry as tests/evalfloor.py writes it, with the checks given (possibly none)."""
    return {"nodeid": f"tests/test_{eval_name}_accuracy.py::test_x[{nodeid_set}]", "eval": eval_name,
            "set": list(checks)[0]["set"] if checks else nodeid_set, "outcome": outcome, "checks": list(checks),
            "message": message, "private": False}  # fmt: skip


def _ok(set_name, metric):
    return {"set": set_name, "metric": metric, "value": 0.99, "bound": 0.9, "kind": "floor", "op": ">=", "ok": True,
            "unit": "share", "n": None, "interval": None}  # fmt: skip


def test_a_crashed_floor_closes_once_its_set_passes():
    """A floor that crashed (or lacked its input) before stating a check is stored under its test's name; the
    passing run reports the real metrics. Every test of the set passing is what recovers it (TRIAGE.md step 6)."""
    gh = FakeGitHub()
    crashed = _report(_floor("dq", "price-train", "error", message="FileNotFoundError: dim_coffee.parquet"))
    assert _sync(gh, crashed) == [("opened", "eval:dq", 1)]
    passing = _report(_floor("dq", "price-train", "passed", [_ok("price/train", "weighted precision")]))
    assert _sync(gh, passing) == [("closed", "eval:dq", 1)]


def test_a_floor_read_from_its_message_closes_once_it_passes():
    """bag_grams doesn't use floors(): its failure is parsed from the message, and a passing run reports no
    checks at all, so its set passing closes the issue; a set that didn't pass keeps it open."""
    gh = FakeGitHub()
    parsed = {"set": "validation", "metric": "weighted accuracy", "value": 0.94, "bound": 0.956, "kind": "floor",
              "op": ">=", "ok": False, "unit": "share", "n": None, "interval": None}  # fmt: skip
    gate = {"set": None, "metric": "full-population accuracy", "value": 0.94, "bound": 0.95, "kind": "floor",
            "op": ">=", "ok": False, "unit": "share", "n": None, "interval": None}  # fmt: skip
    failed = _report(
        _floor("bag_grams", "validation", "failed", [parsed]),
        {"nodeid": "tests/test_bag_grams_accuracy.py::test_full_population_accuracy_meets_the_gate",
         "eval": "bag_grams", "set": "full_population_accuracy_meets_the_gate", "outcome": "failed",
         "checks": [gate | {"set": "full_population_accuracy_meets_the_gate"}], "message": "", "private": False},
    )  # fmt: skip
    assert _sync(gh, failed) == [("opened", "eval:bag_grams", 1)]
    validation_only = _report(
        _floor("bag_grams", "validation", "passed"),
        {"nodeid": "tests/test_bag_grams_accuracy.py::test_full_population_accuracy_meets_the_gate",
         "eval": "bag_grams", "set": "full_population_accuracy_meets_the_gate", "outcome": "failed",
         "checks": [gate | {"set": "full_population_accuracy_meets_the_gate"}], "message": "", "private": False},
    )  # fmt: skip
    assert _sync(gh, validation_only) == [("updated", "eval:bag_grams", 1)]
    all_pass = _report(
        _floor("bag_grams", "validation", "passed"),
        {"nodeid": "tests/test_bag_grams_accuracy.py::test_full_population_accuracy_meets_the_gate",
         "eval": "bag_grams", "set": "full_population_accuracy_meets_the_gate", "outcome": "passed",
         "checks": [], "message": "", "private": False},
    )  # fmt: skip
    assert _sync(gh, all_pass) == [("closed", "eval:bag_grams", 1)]


def test_ci_never_judges_the_private_evals_eval_floors_owns():
    """The reviewer's loop: eval-floors opens eval:origin; ci's report (whose private floors skipped) must not close
    it, so the next eval-floors run updates it instead of reopening it."""
    gh = FakeGitHub()
    floors_run = _report(_test("origin", "train", "failed", 0.95, 0.959, private=True))
    first = triage.sync(triage.eval_alerts(floors_run, "eval-floors", "R1", "s", True, "private"), gh)
    assert first == [("opened", "eval:origin", 1)]
    # ci's report: an in-git floor passed, the private origin floor skipped, and (before the fix) a unit test's
    # passing floors("origin", ...) call; scope in-git leaves eval:origin alone whatever it holds
    ci_run = _report(
        _test("classifier", "train", "passed", 0.99, 0.985),
        _test("origin", "train", "skipped", private=True),
        _test("origin", "train", "passed", 0.96, 0.959, private=True),
    )
    assert triage.sync(triage.eval_alerts(ci_run, "ci", "R2", "s", False, "in-git"), gh) == []
    assert gh.store[0]["state"] == "open" and gh.comments.get(1) is None
    assert triage.sync(triage.eval_alerts(floors_run, "eval-floors", "R3", "s", True, "private"), gh) == [
        ("updated", "eval:origin", 1)
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
    tests/test_bag_grams_accuracy.py does): the report names both, and each failure is an ::error annotation. A
    unit test that calls `floors` outside a floor file is never reported, and a private floor says so."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "conftest.py").write_text(
        "from tests_floor.evalfloor import (  # noqa: F401\n"
        "    pytest_itemcollected, pytest_runtest_call, pytest_runtest_logreport, pytest_sessionfinish,\n"
        "    pytest_terminal_summary)\n"
        "def pytest_configure(config):\n"
        "    config.addinivalue_line('markers', 'private_eval: a private floor')\n"
    )
    (tests / "test_units.py").write_text(
        "from tests_floor.evalfloor import floors\n"
        "def test_a_unit_test_of_the_helper():\n"
        "    with floors('origin', 'train') as f:\n"
        "        f.at_least('weighted agreement', 0.99, 0.959)\n"
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

        @pytest.fixture
        def broken_teardown():
            yield
            raise RuntimeError("teardown broke")

        def test_teardown_after_a_miss(broken_teardown):
            with floors("demo", "teardown") as f:
                f.at_least("accuracy", 0.5, 0.95)

        def test_full_population_gate():
            assert 0.9412 >= 0.95, "full-population accuracy 0.9412 < 0.95 (README)"

        @pytest.mark.private_eval
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
    assert by["test_skipped"]["outcome"] == "skipped" and by["test_skipped"]["private"] is True
    torn = by["test_teardown_after_a_miss"]  # the teardown error doesn't replace the floor's own failure
    assert torn["outcome"] == "failed" and torn["message"].startswith(
        "eval demo, set teardown: accuracy 50.00%"
    )
    assert by["test_demo[train]"]["private"] is False
    assert not any(
        "test_units" in t["nodeid"] for t in doc["tests"]
    )  # a unit test of the helper isn't a floor
    # the annotation escapes % as GitHub's workflow commands require
    assert (
        "::error title=Eval floor demo / train::eval demo, set train: accuracy 90.00%25 < floor 95.00%25"
        in r.stdout
    )
    assert "::error title=Eval floor demo / full_population_gate::" in r.stdout

    alerts = {a["key"]: a for a in triage.eval_alerts(doc, "ci", None, None)}
    assert alerts["eval:demo"]["firing"]
    assert {c["set"] for c in alerts["eval:demo"]["checks"]} == {"train", "teardown", "full_population_gate"}


def test_ci_opens_no_pipeline_issue_for_a_missing_report():
    assert triage.eval_alerts(None, "ci", None, None, alert_if_missing=False) == []
    alerts = triage.eval_alerts(
        _report(_test("classifier", "train", "passed", 0.99, 0.985)), "ci", None, None, False
    )
    assert [a["key"] for a in alerts] == ["eval:classifier"]


def test_the_alert_jobs_hold_only_issues_write_and_never_a_hub_token():
    """Each alert job: main only, never on a pull request, GITHUB_TOKEN limited to issues (plus reading the one ci
    run's artifact for eval-alerts), no secret; the jobs that hold the Hub token can't write issues; and ci, which
    deploy waits on, has no alerting at all (docs/TRIAGE.md, Permissions and safety)."""
    import yaml

    workflows = ROOT / ".github" / "workflows"
    ci = yaml.safe_load((workflows / "ci.yml").read_text())
    assert "alert" not in ci["jobs"] and "issues" not in (workflows / "ci.yml").read_text()
    for name in ("ci.yml", "eval-floors.yml", "prod-health.yml", "eval-alerts.yml", "importers.yml",
                 "importers-alerts.yml"):  # fmt: skip
        wf = yaml.safe_load((workflows / name).read_text())
        assert wf["permissions"] == {"contents": "read"}, name
        for job_name, job in wf["jobs"].items():
            text = yaml.safe_dump(job)
            if job_name == "alert":
                extra = {"actions": "read"} if name == "eval-alerts.yml" else {}
                assert job["permissions"] == {"contents": "read", "issues": "write"} | extra, name
                assert "secrets." not in text, name
                if name == "eval-alerts.yml":
                    for cond in ("workflow_run.event == 'push'", "workflow_run.head_branch == 'main'",
                                 "head_repository.full_name == github.repository"):  # fmt: skip
                        assert cond in job["if"], cond
                else:
                    assert "github.ref == 'refs/heads/main'" in job["if"], name
            else:
                assert "issues" not in (job.get("permissions") or {}), (name, job_name)
            if "HF_TOKEN" in text:
                assert job.get("if") == "github.ref == 'refs/heads/main'", (name, job_name)
    alerts = yaml.safe_load((workflows / "eval-alerts.yml").read_text())
    assert set(alerts[True]) == {"workflow_run"} and alerts[True]["workflow_run"]["workflows"] == ["ci"]
    download = next(s for s in alerts["jobs"]["alert"]["steps"] if "download-artifact" in s.get("uses", ""))
    assert download["with"]["run-id"] == "${{ github.event.workflow_run.id }}"  # that run's artifact only


# --- the importers' weekly snapshots (importers-alerts.yml) --------------------------------------------------------

UTC = triage.UTC
IMPORTERS = ["cafe_imports", "interamerican", "royal_coffee"]
CHECK = "https://github.com/o/r/actions/runs/900"


def _at(s):
    return triage.datetime.fromisoformat(s).replace(tzinfo=UTC)


def _weeks(**latest):
    return {"dataset": "found", "latest": {n: latest.get(n) for n in IMPORTERS}}


def _run(created, conclusion="failure", status="completed", event="schedule", n=7):
    return {"event": event, "status": status, "conclusion": conclusion if status == "completed" else None,
            "created_at": created, "html_url": f"https://github.com/o/r/actions/runs/{n}"}  # fmt: skip


def _isync(gh, weeks, runs, now):
    alerts = triage.importer_alerts(weeks, runs, _at(now), CHECK, "abc123")
    return triage.sync(alerts, gh, now="2026-11-19")


@pytest.mark.parametrize(
    ("now", "runs", "due"),
    [
        ("2026-11-12T12:00", [], None),  # before the first scheduled run: no week is due
        ("2026-11-18T17:40", [], None),  # the first run is due, not done: last week, which predates it
        (
            "2026-11-18T19:00",
            [_run("2026-11-18T17:41:00Z", "success")],
            "2026-11-16",
        ),  # it completed: judged now
        ("2026-11-18T19:00", [_run("2026-11-18T17:41:00Z", status="in_progress")], None),  # still running
        ("2026-11-18T21:38", [], "2026-11-16"),  # no run, and the grace after 17:37 has passed
        ("2026-11-19T09:23", [], "2026-11-16"),  # Thursday's check
        ("2026-11-23T09:00", [], "2026-11-16"),  # the next Monday: still that week until its Wednesday
        ("2026-11-26T09:23", [], "2026-11-23"),
    ],
)
def test_the_due_week_follows_the_wednesday_schedule(now, runs, due):
    got = triage.importers_due_week(_at(now), runs)
    assert (got.isoformat() if got else None) == due


def test_a_failed_scheduled_run_opens_one_issue_with_keys_and_statuses_only():
    """The week's scheduled run failed at Royal: the check that follows it (workflow_run) opens the issue at once.
    Its text is importer keys, statuses, Mondays and run links: nothing from a lot, no store address."""
    gh = FakeGitHub()
    weeks = _weeks(cafe_imports="2026-11-16", interamerican="2026-11-16")
    runs = [_run("2026-11-18T17:52:10Z", "failure")]
    assert _isync(gh, weeks, runs, "2026-11-18T19:30") == [("opened", "importers:weekly", 1)]
    issue = gh.store[0]
    assert issue["title"] == "[triage] importers: a weekly snapshot is missing"
    assert set(issue["labels"]) == {"triage", "importers", "severity:warning"}
    body = issue["body"]
    assert "<!-- triage-key: importers:weekly -->" in body
    assert "| Importer | Status | Last stored week |" in body
    assert "| `royal_coffee` | missing 1 week | none |" in body
    assert "| `cafe_imports` | ok | 2026-11-16 |" in body
    assert "royal_coffee: no stored week for 2026-11-16. The scheduled run for the week of 2026-11-16 ended " \
        "`failure`." in body  # fmt: skip
    assert "https://github.com/o/r/actions/runs/7" in body and CHECK in body
    assert "never dispatch or re-run for it" in body
    # a failed run may be its build job alone, which stores every week: the issue doesn't blame the snapshot
    assert "a build failure alone stores every week and has its own issue, `pipeline:importer-build`" in body
    table_and_text = body.lower()
    for word in ("http://", "royalcoffee", "shopify", "products.json", "price"):
        assert word not in table_and_text.replace("https://github.com/o/r/", ""), word


def _results(*records):
    return [
        {"importer": name, "week": week, "status": status, "http_status": code, "phase": phase, "kind": kind}
        for name, week, status, code, phase, kind in records
    ]


@pytest.mark.parametrize(
    ("code", "phase", "kind", "shown"),
    [
        (403, "listing page 1", "http_status", "HTTP 403 at listing page 1"),
        (429, "listing page 2", "http_status", "HTTP 429 at listing page 2"),
        (503, "robots at listing page 1", "http_status", "HTTP 503 at robots at listing page 1"),
        (200, "page", "challenge", "an edge's challenge (HTTP 200) at page"),
        (200, "page", "too_large", "a response over the size cap (HTTP 200) at page"),
        (None, "listing page 1", "unavailable", "store closed or password-protected at listing page 1"),
        (None, "listing page 3", "not_json", "not JSON at listing page 3"),
        (None, "robots at page", "disallowed", "disallowed by robots.txt at robots at page"),
        # a robots.txt disallowing the whole site is a disallow too (importers._kind), never "store closed"
        (
            None,
            "robots at listing page 1",
            "disallowed",
            "disallowed by robots.txt at robots at listing page 1",
        ),
        (None, "listing page 1", "connect", "no connection (DNS, refused, TLS) at listing page 1"),
        (None, "listing page 1", "timeout", "timed out at listing page 1"),
        (None, "listing page 9", "deadline", "the run's deadline passed at listing page 9"),
        (None, None, None, "failed, not a fetch"),
        (403, "page", "made_up", "HTTP 403 at page"),  # a kind outside the set isn't shown
        (None, "page", "http_status", "a failed fetch at page"),  # http_status without a code
    ],
)
def test_the_issue_names_a_failed_fetchs_kind_status_code_and_phase(tmp_path, code, phase, kind, shown):
    """The run's importer-results artifact (importers.public_results) gives the missing importer's failure kind, HTTP
    status and phase, so the owner can tell a block (403) from rate limiting (429), the site down (5xx), a challenge
    or a closed store, without the log."""
    weeks = _weeks(cafe_imports="2026-11-16", interamerican="2026-11-16")
    run = dict(_run("2026-11-18T17:52:10Z", "failure"), id=44)
    results = {"44": [_results(("cafe_imports", "2026-11-16", "stored", None, None, None),
                               ("royal_coffee", "2026-11-16", "failed", code, phase, kind))]}  # fmt: skip
    gh = FakeGitHub()
    alerts = triage.importer_alerts(weeks, [run], _at("2026-11-18T19:30"), CHECK, "abc123", results)
    triage.sync(alerts, gh, now="2026-11-19")
    body = gh.store[0]["body"]
    assert "| Importer | Status | Last stored week | Failed fetch |" in body
    assert f"| `royal_coffee` | missing 1 week | none | {shown} |" in body
    assert "| `cafe_imports` | ok | 2026-11-16 | – |" in body
    assert f"royal_coffee ({shown}): no stored week for 2026-11-16." in body

    # the same through the command, from the folder the check job fills
    # (results/<run id>/importer-results-<attempt>/results.json)
    (tmp_path / "weeks.json").write_text(json.dumps(weeks))
    (tmp_path / "runs.json").write_text(json.dumps([run]))
    (tmp_path / "results" / "44" / "importer-results-1").mkdir(parents=True)
    (tmp_path / "results" / "44" / "importer-results-1" / "results.json").write_text(
        json.dumps(results["44"][0])
    )
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importers", str(tmp_path), "--dry-run",
         "--now", "2026-11-19T09:23"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert f"| `royal_coffee` | missing 1 week | none | {shown} |" in r.stdout


def test_the_kinds_and_phases_match_the_importers():
    from coffeedata import importers

    assert set(triage.FETCH_KINDS) == set(importers.FETCH_KINDS)
    assert triage._PHASE.pattern == importers.PHASE.pattern


def test_the_latest_run_that_tried_an_importer_decides_what_the_issue_shows():
    weeks = _weeks(cafe_imports="2026-11-16")
    runs = [
        dict(_run("2026-11-18T17:52:10Z", "failure"), id=1),
        dict(_run("2026-11-18T20:10:00Z", "failure", event="workflow_dispatch", n=8), id=2),
        dict(_run("2026-11-11T17:50:00Z", "failure"), id=0),  # last week's run
    ]
    results = {
        "1": [_results(("royal_coffee", "2026-11-16", "failed", 403, "listing page 1", "http_status"),
                       ("interamerican", "2026-11-16", "failed", 503, "page", "http_status"))],
        # the newer dispatch: interamerican failed again, differently; Royal is skipped on a dispatch
        "2": [_results(("interamerican", "2026-11-16", "failed", 429, "page", "http_status"),
                       ("royal_coffee", "2026-11-16", "skipped", None, None, None))],
        "3": [_results(("royal_coffee", "2026-11-16", "failed", 500, "page", "http_status"))],  # not a listed run
        "0": [_results(("cafe_imports", "2026-11-09", "failed", 403, "page", "http_status"))],
    }  # fmt: skip
    week = triage.date(2026, 11, 16)
    failures = triage.importer_failures(results, runs, week)
    assert failures == {"royal_coffee": "HTTP 403 at listing page 1", "interamerican": "HTTP 429 at page"}
    assert triage.importer_failures(results, runs, triage.date(2026, 11, 9)) == {
        "cafe_imports": "HTTP 403 at page"
    }
    # the newest run left no results (killed at its timeout, or its artifact expired): an older code isn't shown
    assert triage.importer_failures({"1": results["1"]}, runs, week) == {}
    # a newer run that stored the importer: no failure shown for it
    stored = dict(results, **{"2": [_results(("interamerican", "2026-11-16", "stored", None, None, None))]})
    assert triage.importer_failures(stored, runs, week) == {"royal_coffee": "HTTP 403 at listing page 1"}
    # malformed files and values: never shown as they are
    bad = {"1": None, "2": [[{"importer": "royal_coffee", "week": "2026-11-16", "status": "failed",
                               "http_status": "https://shop.example/", "phase": "at https://shop.example/x",
                               "kind": "https://shop.example/"}]]}  # fmt: skip
    assert triage.importer_failures(bad, runs, week) == {"royal_coffee": "failed, not a fetch"}
    assert triage.importer_failures(None, None, week) == {}
    alerts = triage.importer_alerts(weeks, runs, _at("2026-11-18T20:30"), CHECK, "abc123", results)
    (weekly,) = [a for a in alerts if a["key"] == "importers:weekly"]
    assert weekly["summary"].startswith(
        "interamerican (HTTP 429 at page), royal_coffee (HTTP 403 at listing page 1): no stored week for 2026-11-16."
    )


def _attempt(folder, run_id, name, records):
    """One downloaded artifact, as `gh run download --pattern` leaves it: <run id>/<artifact name>/results.json."""
    d = folder / "results" / str(run_id) / name
    d.mkdir(parents=True)
    (d / "results.json").write_text(records if isinstance(records, str) else json.dumps(records))


ROYAL_403 = ("royal_coffee", "2026-11-16", "failed", 403, "listing page 1", "http_status")
ROYAL_SKIPPED = ("royal_coffee", "2026-11-16", "skipped", None, None, None)


def test_a_rerun_keeps_the_first_attempts_results_so_royals_code_is_still_shown(tmp_path):
    """#195: "Re-run failed jobs" on the scheduled run never fetches Royal, so attempt 2's results say `skipped`.
    Each attempt has its own artifact (importer-results-<run attempt>), so attempt 1's HTTP status is still read
    and shown; with one overwritten artifact the cell went blank."""
    week = triage.date(2026, 11, 16)
    run = dict(_run("2026-11-18T17:52:10Z", "failure"), id=44)
    first = _results(ROYAL_403, ("interamerican", "2026-11-16", "failed", 503, "page", "http_status"))
    rerun = _results(ROYAL_SKIPPED, ("interamerican", "2026-11-16", "failed", 429, "page", "http_status"))
    _attempt(tmp_path, 44, "importer-results-1", first)
    _attempt(tmp_path, 44, "importer-results-2", rerun)
    (tmp_path / "results" / "44" / "something-else").mkdir()  # not ours: ignored
    results = triage.read_results(tmp_path / "results")
    assert results == {"44": [rerun, first]}  # the newest attempt first
    # Royal: skipped on the re-run, so its first attempt's code; interamerican: the re-run tried it again
    assert triage.importer_failures(results, [run], week) == {
        "royal_coffee": "HTTP 403 at listing page 1",
        "interamerican": "HTTP 429 at page",
    }
    # what one overwritten artifact left: the re-run's results alone, and no code for Royal
    assert "royal_coffee" not in triage.importer_failures({"44": [rerun]}, [run], week)
    # a re-run that stored an importer: no failure is shown for it, and Royal's is kept
    mended = _results(ROYAL_SKIPPED, ("interamerican", "2026-11-16", "stored", None, None, None))
    assert triage.importer_failures({"44": [mended, first]}, [run], week) == {
        "royal_coffee": "HTTP 403 at listing page 1"
    }

    # the same through the command, from the folder the check job fills
    (tmp_path / "weeks.json").write_text(json.dumps(_weeks(cafe_imports="2026-11-16")))
    (tmp_path / "runs.json").write_text(json.dumps([run]))
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importers", str(tmp_path), "--dry-run",
         "--now", "2026-11-19T09:23"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "| `royal_coffee` | missing 1 week | none | HTTP 403 at listing page 1 |" in r.stdout
    assert "| `interamerican` | missing 1 week | none | HTTP 429 at page |" in r.stdout


def test_attempts_are_read_newest_first_whatever_their_names_sort_as(tmp_path):
    """Attempt 10 is newer than attempt 9 (by number, not by name). The artifact's name before attempts were kept
    (importer-results, under --pattern or --name) is the oldest. An attempt whose file can't be read ends the
    search: its outcome is unknown, so an older attempt's code isn't shown."""
    week = triage.date(2026, 11, 16)
    run = dict(_run("2026-11-18T17:52:10Z", "failure"), id=7)
    at = lambda code: _results(("interamerican", "2026-11-16", "failed", code, "page", "http_status"))  # noqa: E731
    for name, code in (("importer-results-9", 409), ("importer-results-10", 410), ("importer-results", 400)):
        _attempt(tmp_path, 7, name, at(code))
    results = triage.read_results(tmp_path / "results")
    assert [a[0]["http_status"] for a in results["7"]] == [410, 409, 400]
    assert triage.importer_failures(results, [run], week) == {"interamerican": "HTTP 410 at page"}

    # the layout `--name importer-results` left (results.json directly under the run), still read
    (tmp_path / "results" / "8").mkdir()
    (tmp_path / "results" / "8" / "results.json").write_text(json.dumps(at(408)))
    (tmp_path / "results" / "9").mkdir()  # a run whose download found nothing: no attempts
    results = triage.read_results(tmp_path / "results")
    assert results["8"] == [at(408)] and results["9"] == []

    # attempt 2's file is cut short: unknown, so attempt 1's Royal code isn't offered as the reason
    _attempt(tmp_path, 5, "importer-results-1", _results(ROYAL_403))
    _attempt(tmp_path, 5, "importer-results-2", '[{"importer": "royal_co')
    results = triage.read_results(tmp_path / "results")
    assert results["5"] == [None, _results(ROYAL_403)]
    assert triage.importer_failures(results, [dict(run, id=5)], week) == {}
    assert triage.read_results(tmp_path / "none") == {}


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "startup_failure"])
def test_a_newer_run_that_never_ran_doesnt_blank_every_cell(tmp_path, conclusion):
    """#195: a dispatch cancelled while queued (or skipped, or one that failed to start) tried no importer and left
    no results. It is passed over, so the scheduled run's failures are still shown; before, it was the newest run
    without results and ended the search, blanking every importer's cell. Its artifacts are still asked for: its
    conclusion alone doesn't say it left none."""
    week = triage.date(2026, 11, 16)
    sched = dict(_run("2026-11-18T17:52:10Z", "failure"), id=100)
    never_ran = dict(_run("2026-11-19T10:00:00Z", conclusion, event="workflow_dispatch", n=8), id=200)
    runs = [never_ran, sched]
    assert [r["id"] for r in triage.importer_result_runs(runs, week)] == [200, 100]
    results = {
        "100": [_results(ROYAL_403, ("interamerican", "2026-11-16", "failed", 503, "page", "http_status"))]
    }
    expected = {"royal_coffee": "HTTP 403 at listing page 1", "interamerican": "HTTP 503 at page"}
    assert triage.importer_failures(results, runs, week) == expected
    assert (
        triage.importer_failures(dict(results, **{"200": []}), runs, week) == expected
    )  # as read_results gives it
    alerts = triage.importer_alerts(
        _weeks(cafe_imports="2026-11-16"), runs, _at("2026-11-19T10:30"), CHECK, "abc123", results
    )
    (weekly,) = [a for a in alerts if a["key"] == "importers:weekly"]
    assert {c["name"]: c.get("fetch") for c in weekly["checks"]} == {
        "cafe_imports": None,
        "interamerican": "HTTP 503 at page",
        "royal_coffee": "HTTP 403 at listing page 1",
    }
    # the check job asks for both runs' artifacts
    (tmp_path / "runs.json").write_text(json.dumps(runs))
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importer-result-runs", str(tmp_path / "runs.json"),
         "--now", "2026-11-19T10:30"],
        capture_output=True, text=True, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0 and r.stdout.split() == ["200", "100"], r.stderr


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "startup_failure"])
def test_a_cancelled_rerun_of_the_scheduled_run_still_shows_the_first_attempts_code(tmp_path, conclusion):
    """Review of #203: a run's conclusion is its latest attempt's. Attempt 1 of the scheduled run failed at Royal with
    a code and uploaded its results; a re-run was then cancelled before its snapshot, so the run reads `cancelled`
    and attempt 2 left nothing. The run is judged by its artifacts: attempt 1's code is still shown."""
    week = triage.date(2026, 11, 16)
    sched = dict(_run("2026-11-18T17:52:10Z", conclusion), id=100)
    assert [r["id"] for r in triage.importer_result_runs([sched], week)] == [100]
    _attempt(tmp_path, 100, "importer-results-1", _results(ROYAL_403))  # attempt 2 left no artifact
    results = triage.read_results(tmp_path / "results")
    assert triage.importer_failures(results, [sched], week) == {"royal_coffee": "HTTP 403 at listing page 1"}
    # a later dispatch that never ran doesn't change that
    later = dict(_run("2026-11-19T10:00:00Z", "cancelled", event="workflow_dispatch", n=8), id=200)
    assert triage.importer_failures(results, [later, sched], week) == {
        "royal_coffee": "HTTP 403 at listing page 1"
    }

    # through the commands: the run's artifacts are asked for, and the issue shows the code
    (tmp_path / "runs.json").write_text(json.dumps([sched]))
    (tmp_path / "weeks.json").write_text(
        json.dumps(_weeks(cafe_imports="2026-11-16", interamerican="2026-11-16"))
    )
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    triage_cmd = [sys.executable, "-m", "coffeedata.triage"]
    r = subprocess.run(
        [*triage_cmd, "importer-result-runs", str(tmp_path / "runs.json"), "--now", "2026-11-19T09:23"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0 and r.stdout.split() == ["100"], r.stderr
    r = subprocess.run(
        [*triage_cmd, "importers", str(tmp_path), "--dry-run", "--now", "2026-11-19T09:23"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "| `royal_coffee` | missing 1 week | none | HTTP 403 at listing page 1 |" in r.stdout
    assert f"ended `{conclusion}`" in r.stdout


def test_a_run_cancelled_part_way_through_its_snapshot_is_read_for_what_it_recorded(tmp_path):
    """Review of #203: `cancelled` also covers a run stopped while its snapshot ran. Its upload step (`if: always()`)
    left results, so it tried those importers: its results are used, and an older run's code isn't shown instead."""
    week = triage.date(2026, 11, 16)
    sched = dict(_run("2026-11-18T17:52:10Z", "failure"), id=100)
    cancelled = dict(_run("2026-11-19T10:00:00Z", "cancelled", event="workflow_dispatch", n=8), id=200)
    runs = [cancelled, sched]
    _attempt(tmp_path, 100, "importer-results-1",
             _results(ROYAL_403, ("interamerican", "2026-11-16", "failed", 503, "page", "http_status"),
                      ("cafe_imports", "2026-11-16", "failed", 500, "page", "http_status")))  # fmt: skip
    # the dispatch: interamerican failed differently, cafe_imports was stored, Royal skipped; then it was cancelled
    _attempt(tmp_path, 200, "importer-results-1",
             _results(ROYAL_SKIPPED, ("interamerican", "2026-11-16", "failed", 429, "page", "http_status"),
                      ("cafe_imports", "2026-11-16", "stored", None, None, None)))  # fmt: skip
    results = triage.read_results(tmp_path / "results")
    assert triage.importer_failures(results, runs, week) == {
        "royal_coffee": "HTTP 403 at listing page 1",  # skipped by the dispatch: the scheduled run's
        "interamerican": "HTTP 429 at page",  # the cancelled run's own, not the older 503
    }  # and cafe_imports, which the cancelled run stored, shows no failure
    # a cancelled run whose results file was cut short: unknown, so nothing older is offered
    (tmp_path / "results" / "200" / "importer-results-1" / "results.json").write_text('[{"importer": "inter')
    assert triage.importer_failures(triage.read_results(tmp_path / "results"), runs, week) == {}


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "success", None])
def test_a_newer_run_that_started_and_left_no_results_still_ends_the_search(conclusion):
    """Only a run that never ran (by its conclusion) and left nothing is passed over. One that ended any other way
    and wrote nothing (killed at its timeout, stopped at the token check, or its artifact expired) has an unknown
    outcome: an older run's code isn't shown."""
    week = triage.date(2026, 11, 16)
    sched = dict(_run("2026-11-18T17:52:10Z", "failure"), id=100)
    newer = dict(_run("2026-11-19T10:00:00Z", conclusion, event="workflow_dispatch", n=8), id=200)
    assert [r["id"] for r in triage.importer_result_runs([newer, sched], week)] == [200, 100]
    assert triage.importer_failures({"100": [_results(ROYAL_403)]}, [newer, sched], week) == {}
    assert triage.importer_failures({"100": [_results(ROYAL_403)], "200": []}, [newer, sched], week) == {}


def test_a_scheduled_run_that_never_ran_shows_no_code_and_the_issue_says_how_it_ended():
    """The week's scheduled run was cancelled and left nothing: no code to show, and the summary gives its
    conclusion."""
    week = triage.date(2026, 11, 16)
    sched = dict(_run("2026-11-18T17:52:10Z", "cancelled"), id=100)
    assert triage.importer_result_runs([sched], week) == [sched]  # its artifacts are still asked for
    assert triage.importer_failures({"100": []}, [sched], week) == {}
    alerts = triage.importer_alerts(_weeks(), [sched], _at("2026-11-18T19:30"), CHECK, "abc123", {})
    (weekly,) = [a for a in alerts if a["key"] == "importers:weekly"]
    assert weekly["firing"] and "ended `cancelled`" in weekly["summary"]
    assert all("fetch" not in c for c in weekly["checks"])


def test_the_check_and_the_alert_judge_the_same_due_week(tmp_path):
    """#195: the check job works out the due week once and records the time it judged at (check.json); the alert job
    judges at that time, not its own clock. Near the grace boundary (four hours after the schedule) two clocks gave
    two weeks: the alert asked for a week whose results the check hadn't downloaded, and every cell was blank."""
    last = dict(_run("2026-11-18T17:52:10Z", "failure"), id=100)  # the week of 2026-11-16
    runs = [last]  # no scheduled run yet in the week of 2026-11-23
    (tmp_path / "runs.json").write_text(json.dumps(runs))
    (tmp_path / "weeks.json").write_text(
        json.dumps(_weeks(cafe_imports="2026-11-16", interamerican="2026-11-16"))
    )
    _attempt(tmp_path, 100, "importer-results-1", _results(ROYAL_403))
    before, after = "2026-11-25T21:36:50", "2026-11-25T21:37:40"  # either side of 17:37 + 4 h
    assert triage.importers_due_week(_at(before), runs) == triage.date(2026, 11, 16)
    assert triage.importers_due_week(_at(after), runs) == triage.date(2026, 11, 23)

    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    triage_cmd = [sys.executable, "-m", "coffeedata.triage"]
    r = subprocess.run(
        [*triage_cmd, "importer-result-runs", str(tmp_path / "runs.json"), "--now", before,
         "--check-out", str(tmp_path / "check.json")],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0 and r.stdout.split() == ["100"], r.stderr
    # a time, nothing else
    assert json.loads((tmp_path / "check.json").read_text()) == {"now": "2026-11-25T21:36:50+00:00"}
    assert triage.check_time(tmp_path) == _at(before)
    assert triage.check_record(_at(after)) == {"now": "2026-11-25T21:37:40+00:00"}

    # the alert job, a minute later by its own clock: it judges at the check's time, so the same week
    importers_cmd = [*triage_cmd, "importers", str(tmp_path), "--dry-run"]
    r = subprocess.run(importers_cmd, capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "due week: 2026-11-16" in r.stdout
    assert "| `royal_coffee` | missing 1 week | none | HTTP 403 at listing page 1 |" in r.stdout
    # an explicit --now still decides (by hand, or a test), and then the week differs
    r = subprocess.run([*importers_cmd, "--now", after], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0 and "due week: 2026-11-23" in r.stdout, r.stderr
    assert "HTTP 403 at listing page 1" not in r.stdout  # that week's results weren't downloaded


@pytest.mark.parametrize(
    "content",
    [None, "", "not json", "[]", '{"due": "2026-11-16"}', '{"now": 5}', '{"now": "soon"}',
     '{"now": "2026-11-25T21:36:50"}'],  # the last has no offset: not what the check writes
)  # fmt: skip
def test_without_a_readable_check_time_the_alert_uses_its_own_clock(tmp_path, content):
    """check.json is best effort (its step is continue-on-error): missing, cut short or holding anything but a time
    with an offset, it is ignored and the alert judges by its own clock, as before."""
    if content is not None:
        (tmp_path / "check.json").write_text(content)
    assert triage.check_time(tmp_path) is None
    (tmp_path / "weeks.json").write_text(json.dumps(_weeks()))
    (tmp_path / "runs.json").write_text("[]")
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importers", str(tmp_path), "--dry-run"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0 and "alert(s) firing" in r.stdout, r.stderr


def test_the_artifacts_read_are_the_due_weeks_runs_and_always_its_scheduled_run(tmp_path, monkeypatch):
    """The check job downloads each attempt's importer-results from the runs `importer-result-runs` prints: the due
    week's completed runs, newest first, at most MAX_RESULT_RUNS, and always its scheduled run, however many
    dispatches followed."""
    monkeypatch.setattr(triage, "MAX_RESULT_RUNS", 3)
    sched = dict(_run("2026-11-18T17:52:10Z", "failure"), id=100)
    dispatches = [
        dict(_run(f"2026-11-19T0{h}:00:00Z", "failure", event="workflow_dispatch"), id=200 + h)
        for h in range(5)
    ]
    running = dict(_run("2026-11-19T09:00:00Z", status="in_progress", event="workflow_dispatch"), id=300)
    old = dict(_run("2026-11-11T17:50:00Z", "failure"), id=50)
    runs = [running, *reversed(dispatches), sched, old]
    week = triage.date(2026, 11, 16)
    assert [r["id"] for r in triage.importer_result_runs(runs, week)] == [204, 203, 202, 100]
    monkeypatch.setattr(triage, "MAX_RESULT_RUNS", 20)
    assert [r["id"] for r in triage.importer_result_runs(runs, week)] == [204, 203, 202, 201, 200, 100]

    (tmp_path / "runs.json").write_text(json.dumps(runs))
    cmd = [sys.executable, "-m", "coffeedata.triage", "importer-result-runs", str(tmp_path / "runs.json"),
           "--now", "2026-11-19T10:00"]  # fmt: skip
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and r.stdout.split() == ["204", "203", "202", "201", "200", "100"], r.stderr
    # no runs listed, or no week due yet: nothing to download
    r = subprocess.run([*cmd[:4], str(tmp_path / "none.json")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and r.stdout == ""


def test_a_run_that_never_happened_is_caught_by_thursdays_check_and_counted_weekly():
    gh = FakeGitHub()
    last_week = _weeks(cafe_imports="2026-11-16", interamerican="2026-11-16", royal_coffee="2026-11-16")
    assert _isync(gh, last_week, [], "2026-11-26T09:23") == [("opened", "importers:weekly", 1)]
    body = gh.store[0]["body"]
    assert "No scheduled importers run started for the week of 2026-11-23 (Wednesday 2026-11-25)." in body
    assert "| `interamerican` | missing 1 week | 2026-11-16 |" in body

    # the same state at the next check: no new comment
    assert _isync(gh, last_week, [], "2026-11-26T15:00") == [("updated", "importers:weekly", 1)]
    assert gh.comments.get(1) is None
    # a second week missed: the statuses change, so a comment says so
    _isync(gh, last_week, [], "2026-12-03T09:23")
    assert "missing 2 weeks" in gh.store[0]["body"] and len(gh.comments[1]) == 1
    assert "royal_coffee missing 2 weeks" in gh.comments[1][0]


def test_a_dispatch_mends_the_html_importers_and_the_issue_closes_on_the_next_full_week():
    gh = FakeGitHub()
    runs = [_run("2026-11-18T17:52:10Z", "failure")]
    _isync(gh, _weeks(), runs, "2026-11-18T19:30")  # nothing stored on the first run
    # a dispatch the same day stores cafe_imports and interamerican (Royal is skipped on a dispatch)
    runs.insert(0, _run("2026-11-18T20:10:00Z", "success", event="workflow_dispatch", n=8))
    fixed = _weeks(cafe_imports="2026-11-16", interamerican="2026-11-16")
    assert _isync(gh, fixed, runs, "2026-11-18T20:30") == [("updated", "importers:weekly", 1)]
    assert (
        gh.store[0]["state"] == "open" and "| `royal_coffee` | missing 1 week | none |" in gh.store[0]["body"]
    )
    # the next scheduled run stores every importer: recovered, closed
    runs.insert(0, _run("2026-11-25T17:45:00Z", "success", n=9))
    full = _weeks(cafe_imports="2026-11-23", interamerican="2026-11-23", royal_coffee="2026-11-23")
    assert _isync(gh, full, runs, "2026-11-25T19:00") == [("closed", "importers:weekly", 1)]
    assert gh.comments[1][-1].startswith("Recovered") and CHECK in gh.comments[1][-1]
    # and a missed week later reopens the same issue, its comment linking the check
    assert _isync(gh, full, runs, "2026-12-03T09:23") == [("reopened", "importers:weekly", 1)]
    assert gh.comments[1][-1].startswith("Firing again") and CHECK in gh.comments[1][-1]


def test_an_unlisted_dataset_opens_the_pipeline_issue_and_leaves_the_importers_issue_alone():
    gh = FakeGitHub()
    _isync(gh, _weeks(), [], "2026-11-19T09:23")
    assert _isync(gh, None, [], "2026-11-26T09:23") == [("opened", "pipeline:importers-health", 2)]
    assert gh.store[0]["state"] == "open" and "missing 1 week" in gh.store[0]["body"]  # untouched
    assert set(gh.store[1]["labels"]) == {"triage", "importers", "severity:warning"}
    # listed again, everything stored: both close
    full = _weeks(cafe_imports="2026-11-23", interamerican="2026-11-23", royal_coffee="2026-11-23")
    assert sorted(_isync(gh, full, [], "2026-11-26T10:00")) == [
        ("closed", "importers:weekly", 1), ("closed", "pipeline:importers-health", 2)
    ]  # fmt: skip


def _firing(weeks, runs, now):
    return [a["key"] for a in triage.importer_alerts(weeks, runs, _at(now), CHECK, None) if a["firing"]]


def test_a_dataset_the_check_cant_judge_opens_the_pipeline_issue_not_every_importer_missing():
    """No token, or the Hub's "not found" once a week is due (a wrong IMPORTERS_URL, a token that can't read the
    dataset): the pipeline issue, never `importers:weekly` with every importer missing."""
    missing = {"dataset": "missing", "latest": dict.fromkeys(IMPORTERS)}
    no_token = {"dataset": "no token", "latest": dict.fromkeys(IMPORTERS)}
    assert _firing(_weeks(), [], "2026-11-13T09:23") == []  # before the first week nothing is due
    assert _firing(missing, [], "2026-11-13T09:23") == []  # and the dataset may not exist yet
    assert _firing(no_token, [], "2026-11-13T09:23") == [
        "pipeline:importers-health"
    ]  # a token is always needed
    for weeks, words in ((missing, "IMPORTERS_URL names the wrong dataset"), (no_token, "no Hub token")):
        alerts = triage.importer_alerts(weeks, [], _at("2026-11-19T09:23"), CHECK, None)
        assert [a["key"] for a in alerts] == ["pipeline:importers-health"]
        assert alerts[0]["firing"] and words in alerts[0]["summary"]
    gh = FakeGitHub()
    _isync(gh, missing, [], "2026-11-19T09:23")
    assert [i["title"] for i in gh.store] == ["[triage] importers-alerts: the check couldn't judge the week"]
    assert "IMPORTERS_URL" in gh.store[0]["body"]


@pytest.mark.parametrize("runs_json", ["", "{not json", None])
def test_an_unlisted_run_history_opens_the_pipeline_issue_and_still_judges_the_weeks(tmp_path, runs_json):
    """The Actions API step failed: runs.json is empty, half-written or absent. The alert job doesn't crash: the
    pipeline issue opens, and the weeks are still judged, by the time alone."""
    (tmp_path / "weeks.json").write_text(json.dumps(_weeks(cafe_imports="2026-11-16")))
    if runs_json is not None:
        (tmp_path / "runs.json").write_text(runs_json)
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importers", str(tmp_path), "--dry-run",
         "--now", "2026-11-19T09:23"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "2 alert(s) firing: pipeline:importers-health, importers:weekly" in r.stdout
    assert "The importers workflow's runs couldn't be listed" in r.stdout
    assert "which run should have stored it is unknown" in r.stdout
    # an empty or unparsable weeks.json is a failed listing too
    (tmp_path / "weeks.json").write_text("")
    assert triage._read_json(tmp_path / "weeks.json") is None
    assert _firing(triage._read_json(tmp_path / "weeks.json"), [], "2026-11-19T09:23") == [
        "pipeline:importers-health"
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("now", "fires"),
    [
        ("2026-11-18T23:00+02:00", False),  # 21:00 UTC Wednesday: inside the grace, nothing due yet
        ("2026-11-18T21:00-05:00", True),  # 02:00 UTC Thursday: the week is due
        ("2026-11-19T02:00", True),  # no offset: UTC
    ],
)
def test_now_converts_its_offset_to_utc(tmp_path, now, fires):
    (tmp_path / "weeks.json").write_text(json.dumps(_weeks()))
    (tmp_path / "runs.json").write_text("[]")
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importers", str(tmp_path), "--dry-run", "--now", now],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert ("1 alert(s) firing: importers:weekly" in r.stdout) == fires, r.stdout


def test_importer_weeks_reads_file_names_only(monkeypatch):
    """The latest week per importer from the dataset's file names; nothing is downloaded. A dataset the token
    can't see reads as missing. A mock HfApi: no network."""
    import httpx2
    import huggingface_hub
    from huggingface_hub.errors import RepositoryNotFoundError

    calls = []

    class FakeApi:
        def __init__(self, token=None):
            pass

        def list_repo_files(self, repo, repo_type):
            calls.append((repo, repo_type))
            if repo == "o/none":
                raise RepositoryNotFoundError(
                    "404",
                    response=httpx2.Response(404, request=httpx2.Request("GET", "https://hub.invalid/")),
                )
            return ["README.md", "raw/week=2026-11-16/importer=cafe_imports.parquet",
                    "raw/week=2026-11-23/importer=cafe_imports.parquet",
                    "raw/week=2026-11-16/importer=royal_coffee.parquet",
                    "raw/week=0000-00-00/importer=interamerican.parquet",  # a schema file
                    "raw/week=2026-13-40/importer=interamerican.parquet",  # not a date
                    "raw/week=2026-11-23/importer=someone_else.parquet"]  # fmt: skip

        def __getattr__(self, name):
            raise AssertionError(f"only file names are listed, not {name}")

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert triage.importer_weeks("hf://datasets/o/importers", IMPORTERS)["dataset"] == "no token"
    assert calls == []  # without a token nothing is asked
    monkeypatch.setenv("HF_TOKEN", "hf_dummy")  # a dummy: the Hub is a mock
    got = triage.importer_weeks("hf://datasets/o/importers@main", IMPORTERS)
    assert got == {"dataset": "found", "latest": {"cafe_imports": "2026-11-23", "interamerican": None,
                                                    "royal_coffee": "2026-11-16"}}  # fmt: skip
    assert calls == [("o/importers", "dataset")]
    assert triage.importer_weeks("hf://datasets/o/none", IMPORTERS)["dataset"] == "missing"


def test_the_importers_command_dry_run_calls_nothing(tmp_path):
    (tmp_path / "weeks.json").write_text(json.dumps(_weeks(cafe_imports="2026-11-16")))
    (tmp_path / "runs.json").write_text(json.dumps([_run("2026-11-18T17:52:10Z")]))
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importers", str(tmp_path), "--dry-run",
         "--now", "2026-11-19T09:23"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "would open issue '[triage] importers: a weekly snapshot is missing'" in r.stdout
    assert "1 alert(s) firing: importers:weekly" in r.stdout
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importers", str(tmp_path / "none"), "--dry-run"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )  # fmt: skip
    assert "1 alert(s) firing: pipeline:importers-health" in r.stdout


def test_the_importers_alerts_workflow_and_the_importers_queue():
    """importers-alerts follows importers runs on main and a Thursday schedule; the Hub token is on its one listing
    step, which runs `importers-weeks` (file names only); the alert job runs the stdlib triage. importers.yml keeps
    every pending run in its queue, so a dispatch can't cancel a pending scheduled run."""
    import yaml

    workflows = ROOT / ".github" / "workflows"
    wf = yaml.safe_load((workflows / "importers-alerts.yml").read_text())
    assert set(wf[True]) == {"workflow_run", "schedule", "workflow_dispatch"}
    assert wf[True]["workflow_run"] == {
        "workflows": ["importers"],
        "types": ["completed"],
        "branches": ["main"],
    }
    assert wf["jobs"]["check"]["permissions"] == {"contents": "read", "actions": "read"}
    hub = [s for s in wf["jobs"]["check"]["steps"] if "HF_TOKEN" in json.dumps(s)]
    assert len(hub) == 1 and "coffeedata.triage importers-weeks" in hub[0]["run"]
    # a failed runs listing before it doesn't skip it: the weeks are still listed and judged
    assert hub[0]["if"] == "${{ !cancelled() }}"
    steps = wf["jobs"]["check"]["steps"]
    assert steps.index(hub[0]) > next(i for i, s in enumerate(steps) if "gh api" in s.get("run", ""))
    assert "importer-snapshot" not in (workflows / "importers-alerts.yml").read_text()
    alert = wf["jobs"]["alert"]
    assert "HF_TOKEN" not in json.dumps(alert) and alert["needs"] == "check"
    assert "python3 -m coffeedata.triage importers " in alert["steps"][-1]["run"]
    importers = yaml.safe_load((workflows / "importers.yml").read_text())
    assert importers["concurrency"] == {"group": "importers", "cancel-in-progress": False, "queue": "max"}
    # the runs listing is written whole or not at all (a failed call leaves no runs.json)
    runs_step = next(s for s in wf["jobs"]["check"]["steps"] if "gh api" in s.get("run", ""))
    assert "runs.json.tmp" in runs_step["run"] and "mv " in runs_step["run"]
    assert "{id, event, status," in runs_step["run"]  # each run's id, for its importer-results artifact
    # the runs' importer-results artifacts (HTTP status codes), best effort, with GITHUB_TOKEN only
    dl = next(s for s in steps if "gh run download" in s.get("run", ""))
    assert dl["continue-on-error"] is True and dl["if"] == "${{ !cancelled() }}"
    assert dl["env"] == {"GH_TOKEN": "${{ github.token }}", "PYTHONPATH": "src"}
    # every attempt's artifact, by pattern (never --name, which reads one), each into its own folder under the run
    assert "--pattern 'importer-results*'" in dl["run"] and "--name" not in dl["run"]
    assert '"$RUNNER_TEMP/importers/results/$id"' in dl["run"]
    assert "python3 -m coffeedata.triage importer-result-runs" in dl["run"]
    assert steps.index(runs_step) < steps.index(dl)
    # the pattern matches the name importers.yml uploads under, and read_results reads that name's folder
    (upload,) = [s for s in importers["jobs"]["snapshot"]["steps"] if "upload-artifact" in s.get("uses", "")]
    name = upload["with"]["name"]
    assert name == triage.RESULTS_ARTIFACT + "-${{ github.run_attempt }}"
    assert triage._RESULTS_DIR.fullmatch(name.replace("${{ github.run_attempt }}", "2")).group(1) == "2"
    # one due week for both jobs: the check records the time it judged at, in the folder the alert job reads
    assert '--check-out "$RUNNER_TEMP/importers/check.json"' in dl["run"]
    assert triage.CHECK_FILE == "check.json" and "--now" not in alert["steps"][-1]["run"]
    assert '"$RUNNER_TEMP/importers"' in alert["steps"][-1]["run"]
    assert "conclusion" in runs_step["run"]  # triage.NEVER_RAN reads each run's conclusion


GH_STUB = """#!/usr/bin/env python3
# A stand-in for `gh run download <id> --repo <repo> --pattern <glob> --dir <dir>`: as gh does with --pattern, it
# writes each matching artifact into its own folder under <dir>, named after the artifact, and fails when none match.
import fnmatch, json, os, pathlib, sys
args = sys.argv[1:]
assert args[:2] == ["run", "download"], args
run, opts = args[2], dict(zip(args[3::2], args[4::2]))
assert set(opts) == {"--repo", "--pattern", "--dir"} and opts["--repo"] == "o/r", opts
found = {n: r for n, r in json.loads(os.environ["ARTIFACTS"]).get(run, {}).items()
         if fnmatch.fnmatch(n, opts["--pattern"])}
if not found:
    sys.exit("no artifact matches any of the names or patterns provided")
for name, records in found.items():
    d = pathlib.Path(opts["--dir"]) / name
    d.mkdir(parents=True)
    (d / "results.json").write_text(json.dumps(records))
"""


def test_the_check_jobs_download_step_feeds_the_alert_every_attempt(tmp_path):
    """The download step's own script (importers-alerts.yml), run with a stand-in `gh`: the scheduled run was re-run
    (two artifacts, Royal skipped on the second), a later dispatch skipped Royal and another was cancelled before
    it ran. The folder it fills gives the alert job Royal's first-attempt code, at the check's time. The step judges by the
    clock, so the runs here are dated in the week that is due now."""
    import yaml

    week = triage.importers_due_week(
        triage.datetime.now(UTC)
    )  # by the time alone; the runs below don't move it
    day = lambda n, at: f"{week + triage.timedelta(days=n)}T{at}Z"  # noqa: E731
    rec = lambda status, code=None, phase=None, kind=None: _results(  # noqa: E731
        ("royal_coffee", week.isoformat(), status, code, phase, kind)
    )

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "importers-alerts.yml").read_text())
    (dl,) = [s for s in wf["jobs"]["check"]["steps"] if "gh run download" in s.get("run", "")]
    bin_dir, temp = tmp_path / "bin", tmp_path / "runner"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(GH_STUB.replace("python3", sys.executable, 1))
    (bin_dir / "gh").chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    folder = temp / "importers"
    folder.mkdir(parents=True)
    runs = [
        dict(_run(day(3, "12:00:00"), "cancelled", event="workflow_dispatch"), id=300),
        dict(_run(day(3, "11:00:00"), "success", event="workflow_dispatch"), id=200),
        dict(_run(day(2, "17:52:10"), "failure"), id=100),
    ]
    (folder / "runs.json").write_text(json.dumps(runs))
    stored = week.isoformat()
    (folder / "weeks.json").write_text(json.dumps(_weeks(cafe_imports=stored, interamerican=stored)))
    artifacts = {
        "100": {
            "importer-results-1": rec("failed", 403, "listing page 1", "http_status"),
            "importer-results-2": rec("skipped"),
            "importers-health": [],  # another artifact's name: the pattern leaves it
        },
        "200": {"importer-results-1": rec("skipped")},
        "400": {"importer-results-1": rec("failed", 500, "page", "http_status")},  # not a listed run
    }
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "RUNNER_TEMP": str(temp), "GITHUB_REPOSITORY": "o/r",
        "PYTHONPATH": str(ROOT / "src"), "ARTIFACTS": json.dumps(artifacts),
    }  # fmt: skip
    r = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", dl["run"]], capture_output=True, text=True, env=env, cwd=ROOT
    )
    assert r.returncode == 0, r.stderr
    if triage.importers_due_week(triage.datetime.now(UTC)) != week:
        pytest.skip("the due week changed while the step ran")
    got = sorted(p.relative_to(folder).as_posix() for p in (folder / "results").rglob("results.json"))
    assert got == [
        "results/100/importer-results-1/results.json",
        "results/100/importer-results-2/results.json",
        "results/200/importer-results-1/results.json",
    ]  # the cancelled run (300) is asked for and has none
    assert "run 300: no importer-results artifact" in r.stdout
    check = json.loads((folder / "check.json").read_text())
    assert set(check) == {"now"}
    assert triage.check_time(folder) is not None

    # the alert job's command over the same folder: it judges at the check's time
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "importers", str(folder), "--dry-run"],
        capture_output=True, text=True, env={k: v for k, v in env.items() if k != "ARTIFACTS"}, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert f"due week: {week}" in r.stdout
    assert "| HTTP 403 at listing page 1 |" in r.stdout and "| `royal_coffee` | missing " in r.stdout
    assert "HTTP 500" not in r.stdout


def test_the_due_week_follows_the_importers_cron():
    """triage's Wednesday 17:37 UTC is importers.yml's cron, and the Thursday check runs after its grace."""
    import yaml

    workflows = ROOT / ".github" / "workflows"
    (cron,) = [c["cron"] for c in yaml.safe_load((workflows / "importers.yml").read_text())[True]["schedule"]]
    minute, hour, dom, month, dow = cron.split()
    assert (dom, month) == ("*", "*")
    assert (int(dow) - 1, int(hour), int(minute)) == triage.IMPORTERS_SCHEDULE  # cron's 3 is Python's 2
    (check,) = [
        c["cron"] for c in yaml.safe_load((workflows / "importers-alerts.yml").read_text())[True]["schedule"]
    ]
    c_minute, c_hour, _, _, c_dow = check.split()
    week = triage.IMPORTERS_FIRST_WEEK
    thursday = triage.datetime.combine(
        week + triage.timedelta(days=int(c_dow) - 1), triage.dtime(int(c_hour), int(c_minute)), UTC
    )
    assert thursday >= triage._scheduled_at(week) + triage.IMPORTERS_GRACE
    assert triage.importers_due_week(thursday) == week
