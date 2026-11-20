"""scripts/tf_error_reasons.py: what the apply workflow may say about why a Terraform step failed. A denied
permission's name and a reason from a fixed list are shown; nothing else from Terraform's message ever is. The two
real cases are this week's (docs/app/RUNNING.md, "Changing the infrastructure"); every other value is invented."""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tf_error_reasons.py"
APPLY = ROOT / ".github" / "workflows" / "terraform-apply.yml"

spec = importlib.util.spec_from_file_location("tf_error_reasons", SCRIPT)
reasons_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reasons_mod)

PROJECT = "coffee-data-tracker"
NUMBER = "866312183550"
EMAIL = "Owner.Person@example.org"
BILLING = "012345-ABCDEF-6789AB"
ENV = {"TF_VAR_owner_email": EMAIL, "TF_VAR_billing_account": BILLING}
HINT = (
    "if the bootstrap on main grants this, run the by-hand bootstrap block (docs/app/RUNNING.md) and run this "
    "workflow again from main"
)

# The log-based alert whose notification rule the apply account couldn't create.
ALERT = f"""
Error: Error creating AlertPolicy: googleapi: Error 403: Permission 'logging.notificationRules.create' denied on resource '//logging.googleapis.com/projects/{PROJECT}/locations/global' (or it may not exist).

  with google_monitoring_alert_policy.usage_dropped[0],
  on usage.tf line 163, in resource "google_monitoring_alert_policy" "usage_dropped":
 163: resource "google_monitoring_alert_policy" "usage_dropped" {{
"""
# The job update Cloud Run refused because the apply account couldn't read the image.
IMAGE = f"""
Error: Error updating Job: googleapi: Error 403: Docker-GetManifest permission_denied: Permission "artifactregistry.repositories.downloadArtifacts" denied on resource "projects/{PROJECT}/locations/us-central1/repositories/registry-coffee" (or it may not exist)

  with google_cloud_run_v2_job.ingest["crawl-shopify"],
  on ingestion.tf line 80, in resource "google_cloud_run_v2_job" "ingest":
"""


def text(log: str, env: dict[str, str] | None = None) -> str:
    return reasons_mod.reasons_text(log, ENV if env is None else env)


def error(message: str, address: str = "google_storage_bucket.plans") -> str:
    return f"\nError: {message}\n\n  with {address},\n  on main.tf line 3, in resource:\n"


def assert_only_allowed(out: str, log: str) -> None:
    """Every line is the header, a hint, or `error N[, address]: reasons[: permissions]` built from the allow-list;
    nothing private from the log is in it."""
    reason = "|".join(re.escape(r) for r in reasons_mod.ORDER)
    line = re.compile(
        rf"  error [0-9]+(?:, [^ :]+)?: (?:{reason})(?:, (?:{reason}))*(?:: [a-zA-Z.]+(?:, [a-zA-Z.]+)*)?"
    )
    fixed = {reasons_mod.HEADER, f"  {reasons_mod.HINT}", f"  {reasons_mod.UNNAMED}"}
    for got in out.splitlines():
        assert got in fixed or line.fullmatch(got), got
        for name in re.findall(
            r"[a-zA-Z]+(?:\.[a-zA-Z]+)+", got.split(": ", 1)[-1] if got.startswith("  error") else ""
        ):
            assert reasons_mod.PERMISSION.fullmatch(name), name
    for private in (
        PROJECT,
        NUMBER,
        EMAIL,
        EMAIL.lower(),
        "example.org",
        BILLING,
        "registry-coffee",
        "//",
        "@",
    ):
        assert private not in out.replace(reasons_mod.HINT, "").replace(reasons_mod.UNNAMED, ""), private


def test_the_two_real_denials_name_their_permissions():
    log = "google_cloud_run_v2_job.ingest: Modifying...\n" + ALERT + IMAGE
    assert text(log) == (
        f"{reasons_mod.HEADER}\n"
        "  error 1, google_monitoring_alert_policy.usage_dropped[0]: 403 permission denied: "
        "logging.notificationRules.create\n"
        '  error 2, google_cloud_run_v2_job.ingest["crawl-shopify"]: 403 permission denied: '
        "artifactregistry.repositories.downloadArtifacts\n"
        f"  {HINT}\n"
    )
    assert reasons_mod.HINT == HINT  # the owner's wording
    assert_only_allowed(text(log), log)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Error creating Service: googleapi: Error 403: Permission 'run.services.create' denied on resource "
         f"'projects/{PROJECT}/locations/us-central1/services/x' (or resource may not exist).",
         "403 permission denied: run.services.create"),
        ("Error setting IAM policy: googleapi: Error 403: Permission 'iam.serviceAccounts.actAs' denied on service "
         f"account registry-coffee-usage@{PROJECT}.iam.gserviceaccount.com (or it may not exist)., forbidden",
         "403 permission denied: iam.serviceAccounts.actAs"),
        (f"googleapi: Error 403: Required 'compute.subnetworks.use' permission for 'projects/{PROJECT}/regions/"
         "us-central1/subnetworks/x', forbidden",
         "403 permission denied: compute.subnetworks.use"),
        (f"googleapi: Error 403: registry-coffee-tf-apply@{PROJECT}.iam.gserviceaccount.com does not have "
         "storage.objects.get access to the Google Cloud Storage object., forbidden",
         "403 permission denied: storage.objects.get"),
        ('googleapi: Error 403: The caller does not have permission\nDetails:\n[{"@type": "type.googleapis.com/'
         'google.rpc.ErrorInfo", "reason": "IAM_PERMISSION_DENIED", "metadata": {"permission": '
         f'"secretmanager.versions.access", "resource": "projects/{NUMBER}/secrets/USAGE_HF_TOKEN"}}}}]',
         "403 permission denied: secretmanager.versions.access"),
        ("rpc error: code = PermissionDenied desc = Permission 'pubsub.topics.create' denied on resource",
         "403 permission denied: pubsub.topics.create"),
        ("Error creating X: googleapi: Error 403: Permission 'a.b.c' denied and Permission 'a.b.d' denied",
         "403 permission denied: a.b.c, a.b.d"),
        (f"Error reading Budget: googleapi: Error 404: Requested entity was not found., notFound {NUMBER}",
         "404 not found"),
        (f"Error creating Role: googleapi: Error 409: A role named tfApplyX in projects/{PROJECT} already exists., "
         "alreadyExists", "409 already exists"),
        ("Error creating Job: googleapi: Error 400: Invalid value at 'job.template' (TYPE_STRING), badRequest",
         "400 invalid argument"),
        ("Error waiting: rpc error: code = InvalidArgument desc = INVALID_ARGUMENT: bad schedule", "400 invalid argument"),
        (f"googleapi: Error 429: Quota exceeded for quota metric 'Write requests' of consumer 'project_number:{NUMBER}'"
         ", rateLimitExceeded", "quota"),
        ("googleapi: Error 403: Quota exceeded for quota group, quotaExceeded", "quota"),  # not a missing permission
        ("Error: RESOURCE_EXHAUSTED: too many instances", "quota"),
        ("Error creating Service: googleapi: Error 503: The service is currently unavailable., backendError",
         "5xx server error"),
        ("Error creating Service: googleapi: Error 500: Internal error encountered.", "5xx server error"),
        ("Provider produced: HTTP response code 502", "5xx server error"),
    ],
)  # fmt: skip
def test_each_reason_in_the_vocabulary(message, expected):
    log = error(message)
    out = text(log)
    assert out.splitlines()[1] == f"  error 1, google_storage_bucket.plans: {expected}"
    assert (f"  {HINT}" in out.splitlines()) == expected.startswith("403"), out
    assert_only_allowed(out, log)


def test_a_denial_that_names_no_permission_points_at_the_audit_log():
    out = text(
        error("Error creating Service: googleapi: Error 403: The caller does not have permission, forbidden")
    )
    assert out.splitlines()[1:] == [
        "  error 1, google_storage_bucket.plans: 403 permission denied",
        f"  {reasons_mod.UNNAMED}",
        f"  {HINT}",
    ]


HOSTILE = [
    "",
    "Apply complete! Resources: 1 added, 0 changed, 0 destroyed.",
    "Permission 'logging.notificationRules.create' denied",  # no Error: line, so not an error's message
    error(f"bad value for {EMAIL.lower()} at 203.0.113.77"),
    error(f'Error when reading or editing Budget "billingAccounts/{BILLING}/budgets/x": denied'),
    error("Error 4031: not a status; Error 40: nor this; error 403 in lower case isn't the provider's"),
    error(f"something about {PROJECT}.iam.gserviceaccount.com and logging.googleapis.com and alerts.tf"),
    error("Unsupported argument; did you mean storage.objects.get?"),  # a name outside a denial's wording
    error("invalid; notFoundation; ALREADY_EXISTS_NOT; myquotaExceededx"),  # words inside other words
    error("x" * 100_000),
    error("\x00\x1b[31m‮ ::error::injected\n::add-mask::x"),
]


@pytest.mark.parametrize("log", HOSTILE)
def test_hostile_or_unknown_input_prints_nothing(log):
    assert text(log) == ""


@pytest.mark.parametrize(
    "name",
    [
        "example.org",  # two parts: a domain, never a permission
        "owner.person",
        "Logging.sinks.create",  # starts upper-case
        "logging.sinks.create.extra.more",  # five parts
        "logging.sinks-x.create",
        "logging.sinks.create2",
        "logging..create",
        "projects/x.y.z",
        "a.b.c@example.org",
        "run.services.create\n::error::x",
        "$(id).a.b",
        "`id`.a.b",
        "a.b.<script>",
        "x" * 200 + ".a.b",
    ],
)
def test_a_name_outside_a_permissions_shape_is_never_shown(name):
    for wording in (
        f"Permission '{name}' denied on resource",
        f'Permission "{name}" denied on resource',
        f"Required '{name}' permission for 'x'",
        f'"permission": "{name}"',
        f"someone does not have {name} access to the object",
    ):
        out = text(error(f"googleapi: Error 403: {wording}"))
        # the status is still a 403; the name is not printed, in whole or in part
        assert out.splitlines()[1] == "  error 1, google_storage_bucket.plans: 403 permission denied", out
        assert_only_allowed(out, wording)


def test_a_permission_shaped_private_value_is_not_shown():
    env = {"TF_VAR_secret": "internal.widgets.launch"}
    out = text(error("googleapi: Error 403: Permission 'internal.widgets.launch' denied"), env)
    assert "internal" not in out and "403 permission denied" in out


@pytest.mark.parametrize(
    "address",
    [
        f'google_monitoring_alert_policy.by_key["{BILLING.replace("-", "")}"]',  # holds a TF_VAR_* value
        'google_x.y["a b"]',
        "google_x.y[`id`]",
        f"projects/{PROJECT}/roles/x",
    ],
)
def test_an_address_the_guard_refuses_is_not_shown(address):
    out = text(error("googleapi: Error 403: Permission 'run.services.create' denied", address))
    assert out.splitlines()[1] == "  error 1: 403 permission denied: run.services.create"
    assert_only_allowed(out, address)


def test_errors_that_match_nothing_are_skipped_but_keep_their_number():
    log = (
        error(f"nothing to see {EMAIL}") + error("googleapi: Error 409: x, alreadyExists") + error("nor here")
    )
    assert text(log).splitlines()[1:] == ["  error 2, google_storage_bucket.plans: 409 already exists"]


def test_at_most_five_permissions_an_error():
    many = " ".join(f"Permission 'a.b.{chr(ord('c') + i) * 3}' denied" for i in range(9))
    out = text(error(f"googleapi: Error 403: {many}"))
    assert out.splitlines()[1].count("a.b.") == reasons_mod.MAX_PERMISSIONS


def test_the_command_line_never_fails_and_never_quotes(tmp_path):
    log = tmp_path / "apply.log"
    log.write_text(ALERT + error(f"bad value for {EMAIL}"))
    run = lambda *args, **kw: subprocess.run(  # noqa: E731
        [sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, env=ENV, **kw
    )
    r = run(str(log))
    assert r.returncode == 0 and "logging.notificationRules.create" in r.stdout and r.stderr == ""
    assert_only_allowed(r.stdout, log.read_text())
    assert run("-", input=IMAGE).stdout.count("downloadArtifacts") == 1
    for args in ([str(tmp_path / "missing.log")], [], ["a", "b"]):
        r = run(*args)
        assert (r.returncode, r.stdout) == (0, "")
    binary = tmp_path / "bin.log"
    binary.write_bytes(b"\xff\xfe Error: \x00" + ALERT.encode())
    assert run(str(binary)).returncode == 0


def test_the_script_is_standard_library_only_with_no_network_or_subprocess():
    tree = ast.parse(SCRIPT.read_text())
    imported = {
        (n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in n.names
    }
    assert imported == {"__future__", "importlib", "os", "re", "sys", "pathlib"}
    source = SCRIPT.read_text()
    for banned in (
        "socket",
        "urllib",
        "http",
        "subprocess",
        "os.system",
        "eval(",
        "exec(",
        'open(argv[0], "w',
    ):
        assert banned not in source.replace("exec_module", ""), banned


# --- the workflow ---------------------------------------------------------------------------------------------------


def _apply_job() -> dict:
    return yaml.safe_load(APPLY.read_text())["jobs"]["apply"]


def _apply_step() -> str:
    return next(
        s for s in _apply_job()["steps"] if s.get("name") == "Check the approved plan, then apply it"
    )["run"]


def test_the_apply_job_gains_no_permission_and_the_shell_stays_strict():
    wf = yaml.safe_load(APPLY.read_text())
    assert wf["permissions"] == {}
    assert _apply_job()["permissions"] == {"contents": "read", "actions": "read", "id-token": "write"}
    assert wf["defaults"]["run"]["shell"] == "bash"  # GitHub runs it as bash --noprofile --norc -eo pipefail
    assert wf["env"]["REASONS"] == "scripts/tf_error_reasons.py"
    script = _apply_step()
    assert 'python3 -I "$REASONS" "$logs/$1.log" || true' in script  # isolated, and never the step's verdict
    assert "set +e" not in script and "set +o pipefail" not in script
    # no call out of the runner was added: the same two curl calls (the plan's download and its delete)
    assert script.count("curl ") == 2 and script.count("gcloud ") == 4


def _functions() -> str:
    """The step's own `why` and `scrubbed` definitions, cut from the workflow."""
    script = _apply_step()
    return script[script.index("why() {") : script.index("stop() {")]


@pytest.mark.parametrize("reasons_script", [str(SCRIPT), "/nonexistent/tf_error_reasons.py"])
def test_the_failure_path_prints_the_summary_then_the_reasons_under_strict_bash(tmp_path, reasons_script):
    """The step's functions, run as GitHub runs them (bash -eo pipefail), on the two real errors with private values
    beside them: the guard's summary, then the reasons, and no value. A missing reasons script changes nothing."""
    logs = tmp_path / "tf-logs"
    logs.mkdir()
    (logs / "apply.log").write_text(ALERT + IMAGE + error(f"bad value for {EMAIL.lower()} near {BILLING}"))
    env = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "GUARD": str(ROOT / "scripts" / "tf_plan_guard.py"),
        "REASONS": reasons_script,
        "S_BILLING_ACCOUNT": BILLING,
        "S_OWNER_EMAIL": EMAIL,
        "S_INGEST_IMAGE": "",
        "S_INGEST_JOB_MEMORY": "",
    }
    body = (
        f'logs="{logs}"\n{_functions()}\nscrubbed apply\nreasons=$(why apply)\necho "after: ${{#reasons}}"\n'
    )
    r = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", body],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("::error::terraform apply failed (3 errors).")
    assert "  resource google_monitoring_alert_policy.usage_dropped[0]" in lines
    found = "logging.notificationRules.create" in r.stdout
    assert found == (reasons_script == str(SCRIPT))
    if found:
        assert lines.index(reasons_mod.HEADER) > lines.index("  at ingestion.tf:80")
        assert f"  {HINT}" in lines and "artifactregistry.repositories.downloadArtifacts" in r.stdout
        assert not lines[-1].endswith(" 0")
    else:
        assert lines[-1] == "after: 0"
    for private in (EMAIL, EMAIL.lower(), BILLING, PROJECT, "example.org"):
        assert private not in r.stdout
