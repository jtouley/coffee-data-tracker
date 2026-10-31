"""The self-hosted `crawler` runner (src/terraform/crawler/) on a public repo: only dispatched or scheduled jobs of
allowed workflows on main may reach it, the hook can't be emptied or switched off, and the live test can't publish."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from coffeedata import livetest

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
TF = ROOT / "src/terraform/crawler"
SCRIPTS = TF / "scripts"
HOOK = SCRIPTS / "job-started.sh"
STARTUP = SCRIPTS / "startup.sh"
SAFE_EVENTS = {"workflow_dispatch", "schedule"}


def _on(wf: dict) -> set[str]:
    on = wf.get("on", wf.get(True))  # PyYAML reads a bare `on:` key as True
    return {on} if isinstance(on, str) else set(on or ())


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


def test_only_dispatch_and_schedule_workflows_target_the_self_hosted_runner():
    found = _crawler_workflows()
    assert ".github/workflows/live-test.yml" in found
    for path, events in found.items():
        assert events <= SAFE_EVENTS, (
            f"{path} targets the crawler runner but is triggered by {events - SAFE_EVENTS}"
        )
    for p in WORKFLOWS.glob("*.y*ml"):  # an expression could pick the label at run time
        assert not re.search(r"runs-on:.*\$\{\{", p.read_text()), p.name


def test_the_hooks_allow_list_is_the_workflows_that_target_the_label():
    hook = HOOK.read_text()
    workflows = re.search(r'^WORKFLOWS="([^"]*)"', hook, re.M)
    events = re.search(r'^EVENTS="([^"]*)"', hook, re.M)
    assert workflows and set(workflows[1].split()) == set(_crawler_workflows())
    assert events and set(events[1].split()) == SAFE_EVENTS
    assert re.search(r'^REF="refs/heads/main"$', hook, re.M)


def test_the_weekly_run_still_uses_a_github_hosted_runner():
    weekly = yaml.safe_load((WORKFLOWS / "weekly.yml").read_text())
    assert all(_runs_on(j) == {"ubuntu-latest"} for j in weekly["jobs"].values())


def test_the_live_test_gets_no_secrets_and_uploads_nothing():
    text = (WORKFLOWS / "live-test.yml").read_text()
    wf = yaml.safe_load(text)
    assert _on(wf) == {"workflow_dispatch"}
    assert wf["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "HF_TOKEN:" not in text and "LAKE_URL" not in text
    for banned in ("upload-artifact", "upload-pages-artifact", "actions/cache", "hf://"):
        assert banned not in text, banned


def test_only_iap_reaches_the_vm_and_no_credential_is_in_terraform():
    tf = "\n".join(p.read_text() for p in TF.glob("*.tf"))
    assert re.findall(r"source_ranges\s*=\s*\[([^\]]*)\]", tf) == ['"35.235.240.0/20"']
    assert re.findall(r"ports\s*=\s*\[([^\]]*)\]", tf) == ['"22"']
    assert not re.search(r'"0\.0\.0\.0/0"', tf)
    assert "google_secret_manager_secret_version" not in tf  # the token's value never goes through Terraform
    assert "google_project_iam" not in tf  # the VM's account gets no project roles
    everything = tf + "".join(p.read_text() for p in SCRIPTS.glob("*.sh"))
    assert not re.search(r"\b(ghp|github_pat|ghs)_[A-Za-z0-9_]{20,}", everything)
    assert not re.search(r"\bA[A-Z0-9]{28}\b", everything)  # the shape of a runner registration token


@pytest.mark.parametrize("script", sorted(p.name for p in SCRIPTS.glob("*.sh")))
def test_the_vm_scripts_parse(script):
    subprocess.run(["bash", "-n", str(SCRIPTS / script)], check=True)


def test_the_job_user_can_change_neither_the_runner_nor_its_hook():
    s = STARTUP.read_text()
    assert "sha256sum -c" in s and "--disableupdate" in s
    assert 'chown -R root:root "$DIR"' in s  # the install, .env and credentials: root's
    assert "ReadOnlyPaths=$DIR" in s and "ReadWritePaths=$DIR/_work $DIR/_diag" in s
    assert "ACTIONS_RUNNER_HOOK_JOB_STARTED=$HOOK" in s and "LIB=/usr/local/lib/crawler-runner" in s
    assert "ExecStartPre=$LIB/check-hook.sh" in s  # no hook, no runner
    assert "KillMode=control-group" in s and "NoNewPrivileges=true" in s
    assert "User=runner" in s and not re.search(r"usermod.*(sudo|docker)", s)
    assert (
        "ExecStart=$DIR/bin/Runner.Listener" in s
    )  # not run.sh, which rewrites run-helper.sh in the install


def test_the_metadata_server_is_dns_only_for_jobs():
    s = STARTUP.read_text()
    block = s[s.index("block_metadata() {") : s.index("\n}\n", s.index("block_metadata() {"))]
    assert re.findall(r"--dport (\d+) -j RETURN", block) == ["53", "53"]
    assert "-A CRAWLER-META -j REJECT" in block and "--uid-owner" in block


def _startup(tmp_path: Path, fetch: str, existing: str | None = None) -> tuple[int, Path]:
    """Run startup.sh's install_hook with `fetch` standing in for the metadata server."""
    dest = tmp_path / "job-started.sh"
    if existing is not None:
        dest.write_text(existing)
    script = f"source {STARTUP}; attr() {{ {fetch}; }}; install_hook {dest}"
    r = subprocess.run(["bash", "-c", script], capture_output=True)
    return r.returncode, dest


def test_a_failed_hook_fetch_never_leaves_an_empty_hook(tmp_path):
    code, dest = _startup(tmp_path, "return 22")  # curl -f failing
    assert code != 0 and not dest.exists()
    assert list(tmp_path.iterdir()) == []  # no temp file left behind either


def test_a_failed_or_partial_fetch_keeps_the_previous_hook(tmp_path):
    good = HOOK.read_text()
    for fetch in ("return 22", "echo '#!/bin/bash'", "true", f"head -c 200 {HOOK}"):
        code, dest = _startup(tmp_path, fetch, existing=good)
        assert code != 0 and dest.read_text() == good, fetch


def test_a_complete_hook_is_installed(tmp_path):
    code, dest = _startup(tmp_path, f"cat {HOOK}")
    assert code == 0 and dest.read_text() == HOOK.read_text() and os.access(dest, os.X_OK)


@pytest.mark.parametrize("content", ["", "#!/bin/bash\nexit 0\n", "#!/bin/bash\n# crawler-runner-hook\nif\n"])
def test_the_unit_refuses_to_start_without_a_good_hook(tmp_path, content):
    hook = tmp_path / "job-started.sh"
    hook.write_text(content)
    r = subprocess.run(["bash", "-c", f"source {STARTUP}; hook_ok {hook}"])
    assert r.returncode != 0
    assert subprocess.run(["bash", "-c", f"source {STARTUP}; hook_ok {tmp_path / 'missing'}"]).returncode != 0
    assert subprocess.run(["bash", "-c", f"source {STARTUP}; hook_ok {HOOK}"]).returncode == 0


def _hook(**env: str) -> int:
    base = {
        "GITHUB_REPOSITORY": "jtouley/coffee-data-tracker",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": "jtouley/coffee-data-tracker/.github/workflows/live-test.yml@refs/heads/main",
    }
    full = {"PATH": os.environ["PATH"]} | base | env
    return subprocess.run(["bash", str(HOOK)], env=full, capture_output=True).returncode


def test_the_pre_job_hook_accepts_a_dispatch_or_schedule_of_an_allowed_workflow_on_main():
    assert _hook() == 0
    assert _hook(GITHUB_EVENT_NAME="schedule") == 0


@pytest.mark.parametrize(
    "env",
    [
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_EVENT_NAME": "pull_request_target"},
        {"GITHUB_EVENT_NAME": "push"},
        {"GITHUB_EVENT_NAME": "workflow_run"},
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
def test_the_pre_job_hook_refuses_everything_else(env):
    assert _hook(**env) != 0


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


def test_the_live_test_refuses_to_run_with_a_token_or_a_remote_lake(no_hf_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    with pytest.raises(SystemExit, match="HF_TOKEN"):
        livetest.publishing_off(tmp_path)
    monkeypatch.delenv("HF_TOKEN")
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


def test_wilson_matches_live_md():
    assert livetest.wilson(5, 100) == (2.2, 11.2)  # phase A, stores with a 429
    assert livetest.wilson(94, 100) == (87.5, 97.2)  # phase A, stores ok
    assert livetest.wilson(0, 0) == (0.0, 100.0)


def test_the_units_hook_check_script_refuses_an_empty_hook(tmp_path):
    check, hook = tmp_path / "check-hook.sh", tmp_path / "job-started.sh"
    subprocess.run(
        ["bash", "-c", f"source {STARTUP}; fn_script {check} 'hook_ok {hook}' hook_ok"], check=True
    )
    hook.write_text("")
    assert subprocess.run([str(check)]).returncode != 0
    hook.write_text(HOOK.read_text())
    assert subprocess.run([str(check)]).returncode == 0
