"""Who can act as the deploy account (docs/app/RUNNING.md, "Which jobs can act as the deploy account").

The deploy provider's Workload Identity condition (src/terraform/deploy.tf) pins the same claims as the bootstrap's
plan and apply providers: the repository's and its owner's numeric ids, a GitHub-hosted runner, main, deploy.yml's
two events, and both workflow_ref and job_workflow_ref to deploy.yml on main. GitHub's token names the file a job is
written in, never the job, so inside deploy.yml the line is `id-token: write`: only the jobs that push or deploy hold
it, and `attest` signs from a reusable workflow (attest.yml), whose job_workflow_ref the condition refuses.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TF = ROOT / "src/terraform"
WORKFLOWS = ROOT / ".github/workflows"
DEPLOY_WORKFLOW = "${var.github_repo}/.github/workflows/deploy.yml@refs/heads/main"
GCP_AUTH = "google-github-actions/auth@"

# The jobs written in deploy.yml that sign in to Google Cloud: they push an image or deploy one.
SIGNS_IN = {"deploy", "deploy-admin", "ingest-image"}


def _code(path: Path) -> str:
    """The file without comments (a `#` inside a string would be cut too; these files have none that matter)."""
    return "\n".join(line.split("#")[0].rstrip() for line in path.read_text().splitlines())


def _block(code: str, header: str) -> str:
    """The body of the block that starts with `header`, up to its closing brace at the same depth."""
    start = code.index(header)
    depth, i = 0, code.index("{", start)
    for j in range(i, len(code)):
        depth += {"{": 1, "}": -1}.get(code[j], 0)
        if depth == 0:
            return code[i + 1 : j]
    raise AssertionError(f"unclosed block {header}")


def _terms(body: str, attribute: str) -> list[str]:
    """The quoted terms of `attribute = join(" && ", [...])`."""
    m = re.search(attribute + r'\s*=\s*join\(" && ", \[(.*?)\]\)', body, re.S)
    assert m, f"{attribute} isn't a join of terms"
    return re.findall(r'^\s*"(.*)",?\s*$', m.group(1), re.M)


def _tf_default(path: Path, name: str) -> str:
    body = _block(_code(path), f'variable "{name}"')
    m = re.search(r'default\s*=\s*"([^"]+)"', body)
    assert m, f"{name} has no default in {path}"
    return m.group(1)


def _provider() -> str:
    return _block(_code(TF / "deploy.tf"), 'resource "google_iam_workload_identity_pool_provider" "github"')


def test_the_deploy_provider_pins_the_workflow_job_and_immutable_ids():
    locals_ = _block(_code(TF / "deploy.tf"), "locals")
    assert re.search(r'deploy_workflow\s*=\s*"' + re.escape(DEPLOY_WORKFLOW) + '"', locals_)
    assert _terms(_provider(), "attribute_condition") == [
        "assertion.repository_id == '${var.github_repository_id}'",
        "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
        "assertion.repository == '${var.github_repo}'",
        "assertion.runner_environment == 'github-hosted'",
        "assertion.ref == 'refs/heads/main'",
        "(assertion.event_name == 'workflow_run' || assertion.event_name == 'workflow_dispatch')",
        "assertion.workflow_ref == '${local.deploy_workflow}'",
        "assertion.job_workflow_ref == '${local.deploy_workflow}'",
    ]


def test_the_deploy_provider_matches_the_bootstraps_repository_claims():
    """The same four repository terms as the bootstrap's providers, and the same ids."""
    boot = _code(TF / "bootstrap/main.tf")
    m = re.search(r'repo_claims\s*=\s*join\(" && ", \[(.*?)\]\)', boot, re.S)
    assert m
    bootstrap_terms = re.findall(r'^\s*"(.*)",?\s*$', m.group(1), re.M)
    assert _terms(_provider(), "attribute_condition")[:4] == bootstrap_terms
    for name in ("github_repo", "github_repository_id", "github_repository_owner_id"):
        assert _tf_default(TF / "variables.tf", name) == _tf_default(TF / "bootstrap/variables.tf", name), (
            name
        )


def test_the_deploy_account_is_granted_by_repository_id_not_name():
    assert re.search(r'"attribute\.repository_id"\s*=\s*"assertion\.repository_id"', _provider())
    grant = _block(
        _code(TF / "deploy.tf"), 'resource "google_service_account_iam_member" "deploy_from_github"'
    )
    assert re.search(r"/attribute\.repository_id/\$\{var\.github_repository_id\}\"", grant)
    assert "attribute.repository/" not in grant
    # The new grant is made after the provider maps repository_id and before the old one goes.
    assert re.search(r"depends_on\s*=\s*\[google_iam_workload_identity_pool_provider\.github\]", grant)
    assert re.search(r"create_before_destroy\s*=\s*true", grant)


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _id_token(permissions) -> bool:
    if permissions in ("write-all",):
        return True
    return isinstance(permissions, dict) and permissions.get("id-token") == "write"


def _holders(wf: dict) -> set[str]:
    """The jobs written in this workflow (not calls of a reusable one) whose token can be minted."""
    return {
        name for name, job in wf["jobs"].items() if _id_token(job.get("permissions")) and "uses" not in job
    }


def _signs_in(job: dict) -> bool:
    return any(GCP_AUTH in str(step.get("uses", "")) for step in job.get("steps", []))


def test_only_the_jobs_that_push_or_deploy_can_sign_in_to_google_cloud():
    wf = _workflow("deploy.yml")
    assert not _id_token(wf.get("permissions")), "deploy.yml's top-level permissions must not grant id-token"
    signs_in = {name for name, job in wf["jobs"].items() if _signs_in(job)}
    assert _holders(wf) == SIGNS_IN
    assert signs_in == SIGNS_IN
    # Every job that signs in names its own permissions (no inherited write-all).
    assert all(isinstance(wf["jobs"][n].get("permissions"), dict) for n in SIGNS_IN)


def test_attest_signs_from_a_reusable_workflow_that_never_signs_in():
    jobs = _workflow("deploy.yml")["jobs"]
    attest = jobs["attest"]
    assert attest["uses"] == "./.github/workflows/attest.yml"
    assert "steps" not in attest
    # A job of deploy.yml that calls a reusable workflow: only attest, so no other job's token has a
    # job_workflow_ref other than deploy.yml's (it would be refused, but it shouldn't exist unnoticed either).
    assert {n for n, j in jobs.items() if "uses" in j} == {"attest"}
    called = _workflow("attest.yml")
    assert set(called[True]) == {"workflow_call"}, "attest.yml runs only when deploy.yml calls it"
    assert called.get("permissions") == {}
    assert not any(_signs_in(j) for j in called["jobs"].values())
    assert GCP_AUTH not in (WORKFLOWS / "attest.yml").read_text()


def test_only_deploy_yml_calls_attest_yml():
    """The app image's check pins the signer (attest.yml) and the build config (deploy.yml); this keeps the two in
    step: no other workflow of this repository can produce an attest.yml signature."""
    callers = {
        wf.name
        for wf in WORKFLOWS.glob("*.y*ml")
        for job in (yaml.safe_load(wf.read_text()).get("jobs") or {}).values()
        if "attest.yml" in str(job.get("uses", ""))
    }
    assert callers == {"deploy.yml"}
    # And nothing calls it any other way (a step's `uses:` can't call a workflow, but a typo would hide here).
    mentions = {wf.name for wf in WORKFLOWS.glob("*.y*ml") if "workflows/attest.yml" in _code(wf)}
    assert mentions <= {"deploy.yml"}


def test_the_check_catches_a_signing_job_holding_the_deploy_token():
    """The rule above, on a deploy.yml where attest is a plain job again with id-token: write."""
    wf = _workflow("deploy.yml")
    wf["jobs"]["attest"] = {
        "needs": "deploy",
        "permissions": {"id-token": "write"},
        "steps": [{"run": "true"}],
    }
    assert _holders(wf) != SIGNS_IN


def test_the_guard_refuses_this_change_so_it_is_applied_by_hand():
    """The plan this change makes (the provider updated in place, the grant replaced) is refused by the guard, so it
    never applies through GitHub: the owner applies it with tf_break_glass.sh and an override."""
    spec = importlib.util.spec_from_file_location("tf_plan_guard", ROOT / "scripts/tf_plan_guard.py")
    assert spec and spec.loader
    guard = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("tf_plan_guard", guard)
    spec.loader.exec_module(guard)

    d = json.loads((ROOT / "tests/fixtures/tf_plan/good.json").read_text())
    pool = "projects/1/locations/global/workloadIdentityPools/github"  # invented
    account = "projects/coffee-data-tracker/serviceAccounts/registry-coffee-deploy@coffee-data-tracker.iam.gserviceaccount.com"
    d["resource_changes"] += [
        {
            "address": "google_iam_workload_identity_pool_provider.github",
            "type": "google_iam_workload_identity_pool_provider",
            "change": {
                "actions": ["update"],
                "before": {"attribute_condition": "assertion.repository == 'a/b'"},
                "after": {"attribute_condition": "assertion.repository_id == '1'"},
                "after_unknown": {},
            },
        },
        {
            "address": "google_service_account_iam_member.deploy_from_github",
            "type": "google_service_account_iam_member",
            "change": {
                "actions": ["create", "delete"],
                "before": {
                    "service_account_id": account,
                    "role": "roles/iam.workloadIdentityUser",
                    "member": f"principalSet://iam.googleapis.com/{pool}/attribute.repository/a/b",
                },
                "after": {
                    "service_account_id": account,
                    "role": "roles/iam.workloadIdentityUser",
                    "member": f"principalSet://iam.googleapis.com/{pool}/attribute.repository_id/1",
                },
                "after_unknown": {},
            },
        },
    ]
    # Exactly these three refuse it, and nothing else: break-glass's override covers them (RUNNING.md).
    failing = {name: bad for name, _, bad in guard.run_guards(guard.load(json.dumps(d))) if bad}
    assert failing == {
        "no-delete-or-replace": ["google_service_account_iam_member.deploy_from_github"],
        "workload-identity": [
            "google_iam_workload_identity_pool_provider.github",
            "google_service_account_iam_member.deploy_from_github",
        ],
        "guarded-accounts": ["google_service_account_iam_member.deploy_from_github"],
    }
