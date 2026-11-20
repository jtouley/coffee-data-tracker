"""GitHub Actions secrets are set on the steps that need them, never in a workflow's or a job's `env` (security
self-assessment L-2). Workflow- and job-level env (and a job container's or service's) is in scope for every step,
third-party actions and `uv sync` included, so a secret there reaches code that never needed it. For the same reason a
secret isn't handed to an action through `with:`, nor to a reusable workflow through `secrets:`, unless allowed here
with its reason."""

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# Any read of the secrets context inside an expression: secrets.X, secrets['X'], toJSON(secrets).
SECRET = re.compile(r"\$\{\{[^}]*\bsecrets\b")

# (workflow, job, where): the only places a secret is allowed outside a step's env, each with its reason.
ALLOWED = {
    # Terraform's billing account and owner email are kept as secrets so they stay out of logs, not because they
    # grant access. Every terraform step of these jobs reads them (as TF_VAR_* in plan, as S_* in apply, where
    # they also scrub error summaries). Moving them belongs to a later terraform PR.
    ("terraform-apply.yml", "plan", "env.TF_VAR_billing_account"),
    ("terraform-apply.yml", "plan", "env.TF_VAR_owner_email"),
    ("terraform-apply.yml", "apply", "env.S_BILLING_ACCOUNT"),
    ("terraform-apply.yml", "apply", "env.S_OWNER_EMAIL"),
}


def _reads_secret(value: object) -> bool:
    return bool(SECRET.search(str(value)))


def exposed_secrets(path: Path) -> set[tuple[str, str, str]]:
    """(workflow, job or "<workflow>", where) for each secret in a shared env, a container's or service's env or
    credentials, a job's `secrets:` (inherit included) or a `uses:` step's `with:`."""
    wf = yaml.safe_load(path.read_text()) or {}
    found = {
        (path.name, "<workflow>", f"env.{k}") for k, v in (wf.get("env") or {}).items() if _reads_secret(v)
    }
    for name, job in (wf.get("jobs") or {}).items():
        places: dict[str, object] = {f"env.{k}": v for k, v in (job.get("env") or {}).items()}
        container = job.get("container")
        if isinstance(container, dict):
            places |= {f"container.env.{k}": v for k, v in (container.get("env") or {}).items()}
            places["container.credentials"] = container.get("credentials")
        for svc, spec in (job.get("services") or {}).items():
            spec = spec or {}
            places |= {f"services.{svc}.env.{k}": v for k, v in (spec.get("env") or {}).items()}
            places[f"services.{svc}.credentials"] = spec.get("credentials")
        found |= {(path.name, name, where) for where, v in places.items() if _reads_secret(v)}
        if "secrets" in job:  # a reusable workflow call: `inherit`, or secrets passed by name
            found.add((path.name, name, "secrets"))
        for i, step in enumerate(job.get("steps") or []):
            if step.get("uses"):
                found |= {
                    (path.name, name, f"steps[{i}].with.{k}")
                    for k, v in (step.get("with") or {}).items()
                    if _reads_secret(v)
                }
    return found


def test_no_workflow_or_job_env_holds_a_secret():
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert files
    found = set().union(*(exposed_secrets(p) for p in files))
    assert found - ALLOWED == set()
    assert found >= ALLOWED, "an allowlist entry no longer applies: remove it"


def test_the_check_sees_every_way_a_secret_can_be_shared(tmp_path):
    wf = tmp_path / "x.yml"
    wf.write_text(
        "env: {A: '${{ secrets.A }}', B: '${{ vars.B }}', J: '${{ toJSON(secrets) }}'}\n"
        "jobs:\n"
        "  j:\n"
        "    env: {C: \"${{ secrets['C'] }}\", N: '${{ vars.secrets_note }}'}\n"
        "    container:\n"
        "      image: x\n"
        "      env: {E: '${{ secrets.E }}'}\n"
        "      credentials: {username: u, password: '${{ secrets.P }}'}\n"
        "    services:\n"
        "      db: {image: y, env: {F: '${{ secrets.F }}'}}\n"
        "    steps:\n"
        "      - {run: 'true', env: {D: '${{ secrets.D }}'}}\n"
        "      - {uses: some/action@v1, with: {token: '${{ secrets.T }}', path: x}}\n"
        "  call:\n"
        "    uses: ./.github/workflows/other.yml\n"
        "    secrets: inherit\n"
    )
    assert exposed_secrets(wf) == {
        ("x.yml", "<workflow>", "env.A"),
        ("x.yml", "<workflow>", "env.J"),
        ("x.yml", "j", "env.C"),
        ("x.yml", "j", "container.env.E"),
        ("x.yml", "j", "container.credentials"),
        ("x.yml", "j", "services.db.env.F"),
        ("x.yml", "j", "steps[1].with.token"),
        ("x.yml", "call", "secrets"),
    }


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
