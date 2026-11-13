"""The importer build's triage issue (coffeedata.importer_build_alert), offline against an in-memory GitHub: opened on a
failed build, updated rather than duplicated, closed by the next build that passes; a status and the run's link only.
No real issue is created."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coffeedata import importer_build_alert as alert
from coffeedata import triage

from .test_triage import FakeGitHub

ROOT = Path(__file__).parent.parent


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(triage, "GitHub", lambda repo, token: fake)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_RUN_ID", "7")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.delenv("TRIAGE_AUTHOR", raising=False)
    return fake


def test_a_failed_build_opens_one_issue_and_the_next_passing_build_closes_it(gh):
    assert alert.main(["failure"]) == 0
    assert len(gh.store) == 1
    issue = gh.store[0]
    assert issue["title"] == "[triage] importer-build: the derived importer offers weren't rebuilt"
    assert issue["labels"] == ["triage", "importers", "severity:warning"] and "importers" in gh.labels_made
    assert "<!-- triage-key: pipeline:importer-build -->" in issue["body"]
    assert "https://github.com/owner/repo/actions/runs/7" in issue["body"]
    assert "raw weeks are stored" in issue["body"]
    # failing again: the same issue, updated, no new comment (nothing new fails)
    assert alert.main(["failure"]) == 0
    assert len(gh.store) == 1 and gh.comments.get(1) is None
    # a build that passed closes it
    assert alert.main(["success"]) == 0
    assert gh.store[0]["state"] == "closed" and gh.comments[1][0].startswith("Recovered")
    # and a later failure reopens the same one
    assert alert.main(["failure"]) == 0
    assert len(gh.store) == 1 and gh.store[0]["state"] == "open"


def test_a_passing_build_with_no_issue_does_nothing(gh):
    assert alert.main(["success"]) == 0
    assert gh.store == [] and gh.labels_made == set()


def test_it_never_touches_another_alerts_issue(gh):
    """Only its own key: an open importers or prod-health issue stays as it is when the build passes."""
    for key in ("importers:weekly", "pipeline:importers-health", "pipeline:prod-health"):
        gh.create(f"[triage] {key}", f"<!-- triage-key: {key} -->\n<!-- triage-failing: [] -->", ["triage"])
    alert.main(["success"])
    alert.main(["failure"])
    assert [i["state"] for i in gh.store[:3]] == ["open"] * 3 and len(gh.store) == 4
    assert all(n > 3 for n in gh.comments)


def test_it_needs_only_the_standard_library_and_runs_without_a_token():
    """The alert job installs nothing (python3 with PYTHONPATH=src): -S leaves out every installed package."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "HF_"))}
    r = subprocess.run(
        [sys.executable, "-S", "-m", "coffeedata.importer_build_alert", "failure", "--dry-run"],
        env=env | {"PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stderr
    assert 'would open issue "[triage] importer-build' in r.stdout
    r = subprocess.run(
        [sys.executable, "-S", "-m", "coffeedata.importer_build_alert", "failure"],
        env=env | {"PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 2 and "GITHUB_TOKEN and GITHUB_REPOSITORY are needed" in r.stderr


def test_the_issue_says_nothing_from_a_lot():
    """It is told only the build job's result: the body is the summary, a status, the run and the commit."""
    a = alert.build_alert("failure", "https://github.com/o/r/actions/runs/1", "abc")
    assert a["checks"] == [{"name": "importer-build", "status": "failed"}]
    assert set(a["context"]) == {"run", "commit"} and "messages" not in a
    assert alert.build_alert("success", None, None)["firing"] is False
