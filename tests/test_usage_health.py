"""The weekly usage check (usage-health.yml): is the collector storing events, and did the check itself run?
`coffeedata usage-build` reads the usage dataset's event file names first; `python -m coffeedata.triage usage` opens
the issues. Offline: a local folder stands for the dataset, and no issue is created."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coffeedata import cli, triage, usage_build

from .test_triage import FakeGitHub

ROOT = Path(__file__).resolve().parents[1]


def _dataset(root: Path, *days: str) -> Path:
    for day in days:
        folder = root / "events" / f"day={day}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "batch=a.parquet").write_bytes(b"")  # names only: no file is ever opened
    (root / "events" / "day=0000-00-00").mkdir(parents=True, exist_ok=True)
    (root / "events" / "day=0000-00-00" / "batch=schema.parquet").write_bytes(b"")  # not a stored day
    (root / "geo").mkdir(exist_ok=True)
    (root / "geo" / "cities.parquet").write_bytes(b"")  # not an event
    return root


def test_the_stored_days_are_read_from_file_names(tmp_path):
    assert usage_build.events_state(str(_dataset(tmp_path / "empty"))) == {"days": 0, "newest": None}
    got = usage_build.events_state(str(_dataset(tmp_path / "some", "2026-11-19", "2026-11-21", "2026-11-20")))
    assert got == {"days": 3, "newest": "2026-11-21"}
    assert usage_build.events_state(str(tmp_path / "no-such-folder")) == {"days": 0, "newest": None}


def test_a_remote_dataset_that_cant_be_listed_says_why_and_names_nothing(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(usage_build.NotListed, match="no Hub token") as e:
        usage_build.events_state("hf://datasets/someone/private-usage")
    assert "someone" not in str(e.value)


def test_a_collector_that_stored_nothing_opens_an_issue_that_says_so():
    """The collector had stored nothing since it was switched on, and nothing said so: the only trace was a log
    line. The alert carries a day and statuses, never a count of events."""
    alert = usage_build.events_alert({"days": 0, "newest": None}, "2026-11-24")
    assert alert["key"] == "prod:usage-events" and alert["firing"] and alert["severity"] == "warning"
    assert "holds no event file: the collector has stored nothing" in alert["summary"]
    assert [c["status"] for c in alert["checks"]] == ["warning", "warning"]
    gh = FakeGitHub()
    assert triage.sync([alert], gh, now="2026-11-24") == [("opened", "prod:usage-events", 1)]
    body = gh.store[0]["body"]
    assert "usage: the collector isn't storing events" in gh.store[0]["title"]
    assert "Read the collector's log in Cloud Logging" in body and "newest stored day: none" in body
    # events arrive: the next check closes it
    fresh = usage_build.events_alert({"days": 2, "newest": "2026-11-30"}, "2026-12-01")
    assert not fresh["firing"] and fresh["summary"] == ""
    assert triage.sync([fresh], gh, now="2026-12-01") == [("closed", "prod:usage-events", 1)]


@pytest.mark.parametrize(
    ("newest", "today", "fires"),
    [
        ("2026-11-21", "2026-11-24", False),
        ("2026-11-20", "2026-11-24", True),
        ("2026-11-24", "2026-11-24", False),
    ],
)
def test_a_collector_that_stopped_storing_fires_after_three_days(newest, today, fires):
    alert = usage_build.events_alert({"days": 5, "newest": newest}, today)
    assert alert["firing"] == fires
    if fires:
        assert f"The newest stored usage event is from {newest}, 4 days before this check" in alert["summary"]
        assert [c["status"] for c in alert["checks"]] == ["ok", "warning"]


def test_the_alerts_window_is_dbts_freshness_window():
    sql = (ROOT / "dbt_usage" / "tests" / "usage_events_are_fresh.sql").read_text()
    assert f"date '{{{{ var('today') }}}}' - {usage_build.EVENTS_FRESH_DAYS}" in sql


def test_usage_build_reports_an_empty_dataset_and_doesnt_try_to_build(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(usage_build, "ROOT", tmp_path.resolve())
    monkeypatch.setenv("COFFEEDATA_USAGE", str(_dataset(tmp_path / "usage")))
    monkeypatch.setattr(usage_build, "build", lambda **kw: pytest.fail("nothing to build from"))
    alerts = tmp_path / "alerts.json"
    cli.main(["usage-build", "--alerts-out", str(alerts)])  # returns: exit 0, the alert carries it
    (alert,) = json.loads(alerts.read_text())  # no detector alert: it measured nothing, and says nothing
    assert alert["key"] == "prod:usage-events" and alert["firing"]
    assert "not attempted: no event is stored" in capsys.readouterr().out


def test_usage_build_exits_non_zero_and_writes_no_alerts_when_the_dataset_cant_be_listed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(usage_build, "ROOT", tmp_path.resolve())
    monkeypatch.setenv("COFFEEDATA_USAGE", "hf://datasets/someone/private-usage")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    alerts = tmp_path / "alerts.json"
    with pytest.raises(SystemExit, match="couldn't be listed: no Hub token") as e:
        cli.main(["usage-build", "--alerts-out", str(alerts)])
    assert not alerts.exists() and "someone" not in str(e.value)


def _triage_usage(path: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    r = subprocess.run(
        [sys.executable, "-m", "coffeedata.triage", "usage", str(path), "--dry-run", "--run-url", "https://x/run/9"],
        capture_output=True, text=True, env=env | {"PYTHONPATH": str(ROOT / "src")}, cwd=ROOT,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_a_check_that_judged_nothing_opens_its_own_issue_and_never_prod_healths(tmp_path):
    out = _triage_usage(tmp_path / "missing.json")
    assert "1 alert(s) firing: pipeline:usage-health" in out and "pipeline:prod-health" not in out
    (tmp_path / "cut.json").write_text("[{")  # a step killed mid-write judged nothing either
    assert "1 alert(s) firing: pipeline:usage-health" in _triage_usage(tmp_path / "cut.json")
    alerts = tmp_path / "alerts.json"
    alerts.write_text(json.dumps([usage_build.events_alert({"days": 0, "newest": None}, "2026-11-24")]))
    out = _triage_usage(alerts)
    assert "1 alert(s) firing: prod:usage-events" in out and "pipeline:prod-health" not in out
    gh = FakeGitHub()
    judged = triage.usage_pipeline_alert(True, None, None)
    assert triage.sync(
        triage.usage_pipeline_alert(False, "https://x/run/9", "abc"), gh, now="2026-11-24"
    ) == [("opened", "pipeline:usage-health", 1)]
    assert "prod-health" in gh.store[0]["labels"] and "HF_READ_TOKEN" in gh.store[0]["body"]
    assert triage.sync(judged, gh, now="2026-12-01") == [("closed", "pipeline:usage-health", 1)]


def test_the_usage_health_workflow_reads_with_the_hub_token_and_alerts_without_it():
    """Weekly, after prod-health; main only; the Hub token on the one step that reads; the alert job holds
    issues: write and no secret; the private database is never uploaded."""
    text = (ROOT / ".github" / "workflows" / "usage-health.yml").read_text()
    wf = yaml.safe_load(text)
    assert wf["permissions"] == {"contents": "read"}
    assert [c["cron"] for c in wf[True]["schedule"]] == ["13 12 * * 2"] and "workflow_dispatch" in wf[True]
    build, alert = wf["jobs"]["build"], wf["jobs"]["alert"]
    assert build["if"] == "github.ref == 'refs/heads/main'" and "permissions" not in build
    reads = [s for s in build["steps"] if "HF_TOKEN" in str(s.get("env"))]
    assert len(reads) == 1 and "coffeedata usage-build --alerts-out" in reads[0]["run"]
    assert reads[0]["env"]["HF_TOKEN"] == "${{ secrets.HF_READ_TOKEN || secrets.HF_TOKEN }}"
    (upload,) = [s for s in build["steps"] if "upload-artifact" in s.get("uses", "")]
    assert upload["with"]["path"].endswith("/usage/alerts.json")  # one file: never build/ or the database
    assert "usage.duckdb" not in text.replace("(build/usage.duckdb)", "") and "usage-dbt.log" not in text
    assert alert["permissions"] == {"contents": "read", "issues": "write"}
    assert "secrets." not in yaml.safe_dump(alert) and "github.ref == 'refs/heads/main'" in alert["if"]
    assert "coffeedata.triage usage" in alert["steps"][-1]["run"]
    for step in (s for job in wf["jobs"].values() for s in job["steps"] if s.get("uses")):
        assert "@" in step["uses"] and len(step["uses"].split("@")[1]) == 40, step["uses"]  # pinned by commit
