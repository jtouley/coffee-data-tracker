"""scripts/tf_bootstrap_preflight.py: the apply job's check that the bootstrap on main has been applied by hand.

Twice an apply failed partway because the apply account lacked a permission main's bootstrap already granted (the
owner's by-hand bootstrap apply hadn't happened yet). The check tests, as the apply account, every permission of the
custom roles main's bootstrap grants it on the project without a condition, before Terraform runs.
"""

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tf_bootstrap_preflight.py"
IAM = ROOT / "src" / "terraform" / "bootstrap" / "iam.tf"
WORKFLOW = ROOT / ".github" / "workflows" / "terraform-apply.yml"

_spec = importlib.util.spec_from_file_location("tf_bootstrap_preflight", SCRIPT)
assert _spec and _spec.loader
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)

TOKEN = "ya29.not-a-real-token-but-must-never-print"

FIXTURE = """
locals {
  plan_member  = "serviceAccount:${google_service_account.tf_plan.email}"
  apply_member = "serviceAccount:${google_service_account.tf_apply.email}"
}

# A comment with condition { in it, and google_project_iam_custom_role.ghost.id: neither counts.
resource "google_project_iam_custom_role" "alpha" {
  role_id     = "alpha"
  title       = "Alpha"
  permissions = ["a.things.create", "a.things.get"] # trailing comment
}

resource "google_project_iam_custom_role" "beta" {
  role_id = "beta"
  title   = "Beta"
  permissions = [
    "b.things.update",
    "a.things.get",
  ]
}

resource "google_project_iam_custom_role" "gamma" {
  role_id     = "gamma"
  title       = "Gamma: conditional only"
  permissions = ["c.things.get"]
}

resource "google_project_iam_custom_role" "delta" {
  role_id     = "delta"
  title       = "Delta: direct grant"
  permissions = ["d.things.delete"]
}

resource "google_project_iam_custom_role" "plan_only" {
  role_id     = "planOnly"
  title       = "The plan account's"
  permissions = ["p.things.list"]
}

resource "google_project_iam_custom_role" "bucket" {
  role_id     = "bucket"
  title       = "On one bucket"
  permissions = ["storage.buckets.update"]
}

resource "google_project_iam_member" "tf_apply" {
  for_each = toset([
    "roles/browser", # predefined: not tested
  ])
  project = var.project_id
  role    = each.value
  member  = local.apply_member
}

resource "google_project_iam_member" "tf_apply_custom" {
  for_each = {
    alpha = google_project_iam_custom_role.alpha.id
    beta  = google_project_iam_custom_role.beta.id
  }
  project = var.project_id
  role    = each.value
  member  = local.apply_member
}

resource "google_project_iam_member" "tf_apply_delta" {
  # condition { title = "only a comment" }
  project = var.project_id
  role    = google_project_iam_custom_role.delta.id
  member  = local.apply_member
}

resource "google_project_iam_member" "tf_apply_conditional" {
  project = var.project_id
  role    = google_project_iam_custom_role.gamma.id
  member  = local.apply_member
  condition {
    title      = "one-bucket"
    expression = "resource.name == 'projects/_/buckets/x'"
  }
}

resource "google_project_iam_member" "tf_plan_custom" {
  project = var.project_id
  role    = google_project_iam_custom_role.plan_only.id
  member  = local.plan_member
}

resource "google_storage_bucket_iam_member" "tf_apply_bucket" {
  bucket = "b"
  role   = google_project_iam_custom_role.bucket.id
  member = local.apply_member
}
"""

FIXTURE_PERMISSIONS = ["a.things.create", "a.things.get", "b.things.update", "d.things.delete"]


# --- Reading iam.tf -----------------------------------------------------------------------------------------------


def test_fixture_unconditioned_project_grants_to_the_apply_account_only():
    assert pf.project_grants(FIXTURE) == ["alpha", "beta", "delta"]
    assert pf.permissions(FIXTURE) == FIXTURE_PERMISSIONS  # sorted, a.things.get once


def test_mains_bootstrap_is_read():
    """Today's main: the three tf_apply_custom roles. tfApplyRepository (on the repository), the bucket roles and the
    plan account's roles are not tested at project level."""
    assert pf.project_grants(IAM.read_text()) == ["tf_service_accounts", "tf_secrets", "tf_log_exclusions"]
    perms = pf.permissions(IAM.read_text())
    assert "logging.notificationRules.create" in perms  # the usage alert's second failure
    assert "iam.serviceAccounts.create" in perms and "secretmanager.secrets.setIamPolicy" in perms
    assert not any(p.startswith(("artifactregistry.", "storage.")) for p in perms)
    assert len(perms) == len(set(perms)) <= pf.BATCH * 3


def test_project_comes_from_the_bootstraps_variables():
    assert pf.project_from(IAM.with_name("variables.tf")) == "coffee-data-tracker"


@pytest.mark.parametrize(
    "change",
    [
        # a role this reader can't place might be a custom one: fail closed, never test less
        ("role    = google_project_iam_custom_role.delta.id", "role    = local.some_role"),
        # a grant of a role that isn't in the file
        ("role    = google_project_iam_custom_role.delta.id", "role    = google_project_iam_custom_role.nope.id"),
        # a permissions list that isn't literal names
        ('permissions = ["d.things.delete"]', "permissions = local.perms"),
        ('permissions = ["d.things.delete"]', 'permissions = ["d.things.delete", local.more]'),
        ('permissions = ["d.things.delete"]', 'permissions = ["d.things.*"]'),
        ('permissions = ["d.things.delete"]', "permissions = []"),
    ],
)
def test_unknown_shapes_fail_closed(change):
    old, new = change
    assert old in FIXTURE
    with pytest.raises(pf.ParseError):
        pf.permissions(FIXTURE.replace(old, new, 1))


# --- Batching -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("n", "sizes"), [(0, []), (1, [1]), (100, [100]), (101, [100, 1]), (250, [100, 100, 50])])
def test_batches_of_at_most_100(n, sizes):
    items = [f"x.y.p{i}" for i in range(n)]
    got = list(pf.batches(items))
    assert [len(b) for b in got] == sizes
    assert [x for b in got for x in b] == items


class FakeApi:
    """Resource Manager's testIamPermissions: answers with the requested permissions the caller holds."""

    def __init__(self, held: set[str], status: int | None = None):
        self.held, self.status, self.calls = held, status, []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        self.calls.append((req.full_url, req.get_method(), dict(req.header_items()), body["permissions"]))
        if self.status:
            raise urllib.error.HTTPError(req.full_url, self.status, "denied", {}, io.BytesIO(b'{"error": {}}'))
        granted = [p for p in body["permissions"] if p in self.held]
        return io.BytesIO(json.dumps({"permissions": granted} if granted else {}).encode())


def test_held_permissions_batches_and_sends_the_token_in_a_header(monkeypatch):
    perms = [f"x.y.p{i:03}" for i in range(230)]
    api = FakeApi(set(perms) - {"x.y.p007", "x.y.p150"})
    monkeypatch.setattr(pf.urllib.request, "urlopen", api)
    held = pf.held_permissions("a-project", TOKEN, perms)
    assert set(perms) - held == {"x.y.p007", "x.y.p150"}
    assert [len(c[3]) for c in api.calls] == [100, 100, 30]
    for url, method, headers, _ in api.calls:
        assert url == "https://cloudresourcemanager.googleapis.com/v1/projects/a-project:testIamPermissions"
        assert method == "POST"
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in url


# --- The check ----------------------------------------------------------------------------------------------------


def _check(monkeypatch, capsys, tmp_path, api, stdin=TOKEN):
    (tmp_path / "iam.tf").write_text(FIXTURE)
    (tmp_path / "variables.tf").write_text('variable "project_id" {\n  type    = string\n  default = "a-project"\n}\n')
    monkeypatch.setattr(pf.urllib.request, "urlopen", api)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = pf.main(["check", str(tmp_path / "iam.tf")])
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    return rc, out.out


def test_all_held_passes(monkeypatch, capsys, tmp_path):
    api = FakeApi(set(FIXTURE_PERMISSIONS))
    rc, out = _check(monkeypatch, capsys, tmp_path, api)
    assert rc == 0 and "::error::" not in out
    assert api.calls[0][0].endswith("/projects/a-project:testIamPermissions")


def test_a_missing_permission_names_it_and_the_fix(monkeypatch, capsys, tmp_path):
    rc, out = _check(monkeypatch, capsys, tmp_path, FakeApi({"a.things.create", "a.things.get"}))
    assert rc == 1
    assert out.startswith("::error::")
    assert "b.things.update, d.things.delete" in out
    assert "a.things.create" not in out
    assert (
        "the bootstrap on main is ahead of what's applied: run the by-hand bootstrap block in docs/app/RUNNING.md, "
        "then re-run this workflow from main" in out
    )
    assert "Nothing was applied." in out


def test_missing_message():
    msg = pf.missing_message(["z.a.b", "logging.notificationRules.create"])
    assert msg.startswith("::error::") and "\n" not in msg
    assert "logging.notificationRules.create, z.a.b" in msg
    assert pf.AHEAD in msg


@pytest.mark.parametrize("status", [401, 403, 500])
def test_an_api_error_fails_with_fixed_text(monkeypatch, capsys, tmp_path, status):
    rc, out = _check(monkeypatch, capsys, tmp_path, FakeApi(set(), status=status))
    assert rc == 2
    assert out.strip() == (
        f"::error::Couldn't test the apply account's permissions on a-project (HTTP {status}). Nothing was applied."
    )


def test_no_token_fails(monkeypatch, capsys, tmp_path):
    api = FakeApi(set())
    rc, out = _check(monkeypatch, capsys, tmp_path, api, stdin="  \n")
    assert rc == 2 and "No access token" in out and not api.calls


def test_permissions_command_prints_mains_list():
    r = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "permissions", str(IAM)], capture_output=True, text=True, check=True
    )
    assert r.stdout.split() == pf.permissions(IAM.read_text())


# --- The workflow -------------------------------------------------------------------------------------------------


def _apply_steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["apply"]["steps"]


def _index(steps: list[dict], pred) -> int:
    found = [i for i, s in enumerate(steps) if pred(s)]
    assert len(found) == 1, found
    return found[0]


def test_the_workflow_runs_the_check_first_after_auth_and_before_terraform():
    steps = _apply_steps()
    auth = _index(steps, lambda s: str(s.get("uses", "")).startswith("google-github-actions/auth@"))
    gcloud = _index(steps, lambda s: str(s.get("uses", "")).startswith("google-github-actions/setup-gcloud@"))
    check = _index(steps, lambda s: "scripts/tf_bootstrap_preflight.py check" in s.get("run", ""))
    setup_tf = _index(steps, lambda s: str(s.get("uses", "")).startswith("hashicorp/setup-terraform@"))
    apply = _index(steps, lambda s: "terraform -chdir=\"$TF_ROOT\" apply" in s.get("run", ""))
    # gcloud gives the token (as the other steps get theirs); nothing else between auth and the check
    assert steps[auth]["with"]["service_account"] == "${{ vars.GCP_TF_APPLY_SA }}"
    assert auth < gcloud < check and check == gcloud + 1 and gcloud == auth + 1
    assert check < setup_tf < apply
    assert "terraform " not in steps[check]["run"]


def test_the_check_runs_with_bash_eo_pipefail_and_masks_the_token():
    wf = yaml.safe_load(WORKFLOW.read_text())
    step = _apply_steps()[_index(_apply_steps(), lambda s: "tf_bootstrap_preflight.py" in s.get("run", ""))]
    # `shell: bash` is GitHub's `bash --noprofile --norc -eo pipefail {0}`
    assert wf["defaults"]["run"]["shell"] == "bash"
    assert "shell" not in step and "shell" not in (wf["jobs"]["apply"].get("defaults") or {}).get("run", {})
    assert "continue-on-error" not in step and "if" not in step
    run = step["run"]
    assert 'echo "::add-mask::$token"' in run
    assert run.index("::add-mask::") < run.index("tf_bootstrap_preflight.py check")
    assert "python3 -I scripts/tf_bootstrap_preflight.py check src/terraform/bootstrap/iam.tf" in run
    # the token only on stdin: never an argument, never in env
    assert "printf '%s' \"$token\" | python3 -I" in run
    assert "env" not in step


def test_only_the_approved_apply_job_can_get_the_apply_token():
    """Why the check isn't in the plan job: that job authenticates as the plan account, and the apply provider's
    condition needs the `terraform-apply` environment claim, which only the approved apply job's token carries."""
    wf = yaml.safe_load(WORKFLOW.read_text())
    plan_auth = [s for s in wf["jobs"]["plan"]["steps"] if "auth@" in str(s.get("uses", ""))]
    assert [s["with"]["service_account"] for s in plan_auth] == ["${{ vars.GCP_TF_PLAN_SA }}"]
    assert wf["jobs"]["apply"]["environment"] == "terraform-apply"
    main_tf = (ROOT / "src" / "terraform" / "bootstrap" / "main.tf").read_text()
    assert "\"assertion.environment == '${var.apply_environment}'\"" in main_tf


def _run_step_body(tmp_path: Path, gcloud: str, python: str) -> subprocess.CompletedProcess:
    """The step's own text under GitHub's bash flags, with stand-ins for gcloud and python3."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, text in (("gcloud", gcloud), ("python3", python)):
        (bindir / name).write_text("#!/bin/sh\n" + text)
        (bindir / name).chmod(0o755)
    for name in ("cat", "sh"):
        target = shutil.which(name)
        assert target
        (bindir / name).symlink_to(target)
    step = _apply_steps()[_index(_apply_steps(), lambda s: "tf_bootstrap_preflight.py" in s.get("run", ""))]
    env = {"PATH": str(bindir), "HOME": str(tmp_path), "STDIN_COPY": str(tmp_path / "stdin")}
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_step_passes_the_token_on_stdin_and_fails_when_the_check_fails(tmp_path):
    r = _run_step_body(tmp_path, f"echo {TOKEN}\n", 'cat > "$STDIN_COPY"\necho "::error::missing"\nexit 1\n')
    assert r.returncode == 1
    assert (tmp_path / "stdin").read_text() == TOKEN
    assert r.stdout.count(TOKEN) == 1 and f"::add-mask::{TOKEN}" in r.stdout  # only the mask command carries it


def test_step_fails_when_no_token_can_be_had(tmp_path):
    r = _run_step_body(tmp_path, "exit 1\n", 'cat > "$STDIN_COPY"\nexit 0\n')
    assert r.returncode != 0
    assert not (tmp_path / "stdin").exists()  # the check never ran
