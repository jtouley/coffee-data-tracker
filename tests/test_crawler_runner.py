"""The self-hosted `crawler` runner (src/terraform/crawler/) on a public repo: only dispatched or scheduled jobs of
allowed workflows may reach it, the live test can't publish, and the User-Agent's contact is opt-in."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from coffeedata import http, livetest

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
TF = ROOT / "src/terraform/crawler"
SCRIPTS = TF / "scripts"
SAFE_EVENTS = {"workflow_dispatch", "schedule"}


def _on(wf: dict) -> set[str]:
    on = wf.get("on", wf.get(True))  # PyYAML reads a bare `on:` key as True
    if isinstance(on, str):
        return {on}
    return set(on or ())


def _runs_on(job: dict) -> set[str]:
    r = job.get("runs-on", [])
    if isinstance(r, dict):
        r = r.get("labels", [])
    return {r} if isinstance(r, str) else set(r)


def _crawler_workflows() -> dict[str, set[str]]:
    out = {}
    for p in sorted(WORKFLOWS.glob("*.y*ml")):
        wf = yaml.safe_load(p.read_text())
        if any(_runs_on(j) & {"crawler", "self-hosted"} for j in wf.get("jobs", {}).values()):
            out[f".github/workflows/{p.name}"] = _on(wf)
    return out


def _tf_default(name: str) -> list[str]:
    body = (TF / "variables.tf").read_text().split(f'variable "{name}"')[1].split("\nvariable ")[0]
    return re.findall(r'"([^"]+)"', body.split("default", 1)[1].split("]", 1)[0])


def test_only_dispatch_and_schedule_workflows_target_the_self_hosted_runner():
    found = _crawler_workflows()
    assert ".github/workflows/live-test.yml" in found
    for path, events in found.items():
        assert events <= SAFE_EVENTS, (
            f"{path} runs on the crawler runner but is triggered by {events - SAFE_EVENTS}"
        )
    # A dynamic runs-on (an expression) could pick the label at run time: none may exist.
    for p in WORKFLOWS.glob("*.y*ml"):
        assert not re.search(r"runs-on:.*\$\{\{", p.read_text()), p.name


def test_the_runners_allow_list_is_exactly_the_workflows_that_target_it():
    assert set(_tf_default("allowed_workflows")) == set(_crawler_workflows())
    assert set(_tf_default("allowed_events")) == SAFE_EVENTS
    assert _tf_default("allowed_refs") == ["refs/heads/main"]


def test_the_weekly_run_still_uses_a_github_hosted_runner():
    weekly = yaml.safe_load((WORKFLOWS / "weekly.yml").read_text())
    assert {_runs_on(j) == {"ubuntu-latest"} for j in weekly["jobs"].values()} == {True}


def test_the_live_test_gets_no_secrets_and_uploads_nothing():
    text = (WORKFLOWS / "live-test.yml").read_text()
    wf = yaml.safe_load(text)
    assert _on(wf) == {"workflow_dispatch"}
    assert wf["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    assert "HF_TOKEN:" not in text and "LAKE_URL" not in text
    for banned in ("upload-artifact", "upload-pages-artifact", "actions/cache", "hf://"):
        assert banned not in text, banned


def test_only_iap_reaches_the_vm_and_no_credential_is_in_terraform():
    tf = "\n".join(p.read_text() for p in TF.glob("*.tf"))
    ranges = re.findall(r"source_ranges\s*=\s*\[([^\]]*)\]", tf)
    assert ranges == ['"35.235.240.0/20"']  # IAP TCP forwarding only
    assert re.findall(r"ports\s*=\s*\[([^\]]*)\]", tf) == ['"22"']
    assert not re.search(r'"0\.0\.0\.0/0"', tf)
    assert "google_secret_manager_secret_version" not in tf  # the token's value never goes through Terraform
    assert "google_project_iam" not in tf  # the VM's account gets no project roles
    assert 'enable-oslogin         = "TRUE"' in tf and 'block-project-ssh-keys = "TRUE"' in tf
    everything = tf + "".join(p.read_text() for p in SCRIPTS.glob("*.sh"))
    assert not re.search(r"\b(ghp|github_pat|ghs)_[A-Za-z0-9_]{20,}", everything)
    assert not re.search(r"\bA[A-Z0-9]{28}\b", everything)  # the shape of a runner registration token


@pytest.mark.parametrize("script", sorted(p.name for p in SCRIPTS.glob("*.sh")))
def test_the_vm_scripts_parse(script):
    subprocess.run(["bash", "-n", str(SCRIPTS / script)], check=True)


def test_startup_pins_and_verifies_the_runner_and_keeps_jobs_off_the_metadata_server():
    s = (SCRIPTS / "startup.sh").read_text()
    assert "sha256sum -c" in s
    assert "--uid-owner" in s and "169.254.169.254" in s
    assert "User=runner" in s and "ACTIONS_RUNNER_HOOK_JOB_STARTED" in s
    assert re.search(r"usermod.*sudo|adduser runner sudo|usermod.*docker", s) is None


HF_NAMES = ("HfApi", "hf_hub_download", "snapshot_download", "upload_file", "upload_folder", "create_commit")


@pytest.fixture
def no_hf_env(monkeypatch):
    """No token, and huggingface_hub and logging put back as they were after publishing_off() replaces them."""
    import huggingface_hub

    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    for name in HF_NAMES:
        monkeypatch.setattr(huggingface_hub, name, getattr(huggingface_hub, name))
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def hook(tmp_path):
    allow = tmp_path / "allow.env"
    allow.write_text(
        "ALLOWED_REPO=jtouley/coffee-data-tracker\nALLOWED_EVENTS='workflow_dispatch schedule'\n"
        "ALLOWED_REFS=refs/heads/main\nALLOWED_WORKFLOWS=.github/workflows/live-test.yml\n"
    )
    script = tmp_path / "job-started.sh"
    script.write_text(
        (SCRIPTS / "job-started.sh").read_text().replace("/etc/crawler-runner/allow.env", str(allow))
    )

    def run(**env: str) -> int:
        base = {
            "GITHUB_REPOSITORY": "jtouley/coffee-data-tracker",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": "jtouley/coffee-data-tracker/.github/workflows/live-test.yml@refs/heads/main",
        }
        full = {"PATH": os.environ["PATH"]} | base | env
        return subprocess.run(["bash", str(script)], env=full, capture_output=True).returncode

    return run


def test_the_pre_job_hook_accepts_a_dispatch_of_an_allowed_workflow_on_main(hook):
    assert hook() == 0
    assert hook(GITHUB_EVENT_NAME="schedule") == 0


@pytest.mark.parametrize(
    "env",
    [
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_EVENT_NAME": "pull_request_target"},
        {"GITHUB_EVENT_NAME": "push"},
        {"GITHUB_EVENT_NAME": ""},
        {"GITHUB_REF": "refs/pull/7/merge"},
        {"GITHUB_REF": "refs/heads/some-branch"},
        {"GITHUB_REPOSITORY": "someone/coffee-data-tracker"},
        {"GITHUB_WORKFLOW_REF": "jtouley/coffee-data-tracker/.github/workflows/ci.yml@refs/heads/main"},
        {"GITHUB_WORKFLOW_REF": "jtouley/coffee-data-tracker/.github/workflows/live-test.yml@refs/heads/x"},
        {"GITHUB_WORKFLOW_REF": "evil/fork/.github/workflows/live-test.yml@refs/heads/main"},
        {"GITHUB_WORKFLOW_REF": ""},
    ],
)
def test_the_pre_job_hook_refuses_everything_else(hook, env):
    assert hook(**env) != 0


def test_the_user_agent_is_unchanged_without_a_contact(monkeypatch):
    monkeypatch.delenv("COFFEEDATA_CONTACT", raising=False)
    assert http.user_agent() == "coffee-data/0.1 (+https://github.com/jtouley/coffee-data-tracker)"
    monkeypatch.setenv("COFFEEDATA_CONTACT", "  ")
    assert http.user_agent() == http.USER_AGENT
    with http.PoliteClient() as c:
        assert c._client.headers["User-Agent"] == http.USER_AGENT


def test_a_contact_goes_inside_the_user_agents_comment(monkeypatch):
    monkeypatch.setenv("COFFEEDATA_CONTACT", "crawler@registry.coffee")
    ua = "coffee-data/0.1 (+https://github.com/jtouley/coffee-data-tracker; crawler@registry.coffee)"
    assert http.user_agent() == ua
    with http.PoliteClient() as c:
        assert c._client.headers["User-Agent"] == ua


@pytest.mark.parametrize("bad", ["a b@c.d", "x)y", "x;y", "x\r\nX-Evil: 1", "(x)"])
def test_a_contact_that_could_break_the_header_is_refused(monkeypatch, bad):
    monkeypatch.setenv("COFFEEDATA_CONTACT", bad)
    with pytest.raises(ValueError):
        http.user_agent()


def test_robots_rules_still_match_with_a_contact(monkeypatch):
    import urllib.robotparser

    rp = urllib.robotparser.RobotFileParser()
    rp.parse(["User-agent: coffee-data", "Disallow: /"])
    monkeypatch.setenv("COFFEEDATA_CONTACT", "crawler@registry.coffee")
    assert not rp.can_fetch(http.user_agent(), "https://shop.example/products.json")
    assert not rp.can_fetch(http.USER_AGENT, "https://shop.example/products.json")


def test_the_live_test_refuses_to_run_with_a_token_or_a_remote_lake(no_hf_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    with pytest.raises(SystemExit, match="HF_TOKEN"):
        livetest.publishing_off(tmp_path)
    monkeypatch.delenv("HF_TOKEN")
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="local folder"):
        livetest.publishing_off("hf://datasets/x/y")


def test_the_live_test_blocks_hugging_face(no_hf_env, monkeypatch, tmp_path):
    import huggingface_hub

    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path))
    livetest.publishing_off(tmp_path / "lake")
    with pytest.raises(RuntimeError, match="blocked"):
        huggingface_hub.HfApi()


def test_the_live_test_picks_200_stores_on_distinct_hosts_and_prints_no_names(
    no_hf_env, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    assert livetest.main(["--lake", str(tmp_path / "lake"), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "picked 200 on distinct hosts" in out
    assert not re.search(r"\.(com|coffee|co|net|org)\b", out)


def test_wilson():
    assert livetest.wilson(5, 100) == (2.2, 11.2)  # live.md, phase A stores with a 429
    assert livetest.wilson(0, 0) == (0.0, 100.0)
