"""GitHub Actions secrets are set on the steps that need them, never in a workflow's or a job's `env` (security
self-assessment L-2). Workflow- and job-level env is in scope for every step, third-party actions and `uv sync`
included, so a secret there reaches code that never needed it."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# (workflow, job, variable): the only secrets allowed in a job's env, each with its reason.
ALLOWED = {
    # Terraform's billing account and owner email are kept as secrets so they stay out of logs, not because they
    # grant access. Every terraform step of these jobs reads them (as TF_VAR_* in plan, as S_* in apply, where
    # they also scrub error summaries), and the terraform workflows aren't changed here.
    ("terraform-apply.yml", "plan", "TF_VAR_billing_account"),
    ("terraform-apply.yml", "plan", "TF_VAR_owner_email"),
    ("terraform-apply.yml", "apply", "S_BILLING_ACCOUNT"),
    ("terraform-apply.yml", "apply", "S_OWNER_EMAIL"),
}


def secrets_in_shared_env(path: Path) -> set[tuple[str, str, str]]:
    """(workflow, job or "<workflow>", variable) for each workflow- or job-level env value that reads a secret."""
    wf = yaml.safe_load(path.read_text()) or {}
    found = {(path.name, "<workflow>", k) for k, v in (wf.get("env") or {}).items() if "secrets." in str(v)}
    for name, job in (wf.get("jobs") or {}).items():
        found |= {(path.name, name, k) for k, v in (job.get("env") or {}).items() if "secrets." in str(v)}
    return found


def test_no_workflow_or_job_env_holds_a_secret():
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert files
    found = set().union(*(secrets_in_shared_env(p) for p in files))
    assert found - ALLOWED == set()
    assert found >= ALLOWED, "an allowlist entry no longer applies: remove it"


def test_the_check_sees_job_and_workflow_level_secrets(tmp_path):
    wf = tmp_path / "x.yml"
    wf.write_text(
        "env: {A: '${{ secrets.A }}', B: '${{ vars.B }}'}\n"
        "jobs:\n"
        "  j:\n"
        "    env: {C: '${{ secrets.C }}'}\n"
        "    steps: [{run: 'true', env: {D: '${{ secrets.D }}'}}]\n"
    )
    assert secrets_in_shared_env(wf) == {("x.yml", "<workflow>", "A"), ("x.yml", "j", "C")}


def test_the_steps_that_run_coffeedata_still_get_their_secrets():
    """Moved, not dropped: each secret is on the step whose coffeedata command reads it."""

    def step(workflow: str, job: str, name: str) -> dict:
        steps = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job]["steps"]
        return next(s for s in steps if s.get("name") == name)

    issue = step("issue-forms.yml", "handle", "Process issue")
    assert "coffeedata" in issue["run"] and issue["env"]["HF_TOKEN"] == "${{ secrets.HF_TOKEN }}"
    weekly = step("weekly.yml", "run", "Weekly run")
    assert "coffeedata weekly" in weekly["run"]
    for key in ("HF_TOKEN", "COFFEEDATA_ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        assert weekly["env"][key] == f"${{{{ secrets.{key} }}}}"
    snowflake = step("weekly.yml", "run", "Load Snowflake")
    assert "load-snowflake" in snowflake["run"]
    assert snowflake["env"]["SNOWFLAKE_PRIVATE_KEY"] == "${{ secrets.SNOWFLAKE_PRIVATE_KEY }}"
    # The step's `if` can't see its own env, so it reads the repository variable.
    assert snowflake["if"] == "vars.SNOWFLAKE_ACCOUNT != ''"
    assert "SNOWFLAKE_PRIVATE_KEY" not in str(weekly["env"])


def test_pages_artifacts_keep_the_well_known_folder():
    """upload-pages-artifact v5 leaves out dot folders unless asked, which would drop /.well-known/security.txt."""
    uploads = [
        s
        for p in WORKFLOWS.glob("*.yml")
        for job in (yaml.safe_load(p.read_text()).get("jobs") or {}).values()
        for s in job.get("steps") or []
        if "upload-pages-artifact" in str(s.get("uses"))
    ]
    assert len(uploads) >= 2
    assert all(s["with"].get("include-hidden-files") is True for s in uploads)
