"""The admin's job list and its Terraform grants agree: admin/jobs.py JOB_NAMES (every job an action can start) is
exactly local.admin_jobs in src/terraform/admin_control.tf (the jobs the admin's account may run with overrides),
and each is a job in local.ingest_jobs. A job added to one and not the other fails here, not in production."""

import re
from pathlib import Path

import pytest

from admin import jobs

TF = Path(__file__).parents[1] / "src" / "terraform"


def test_the_jobs_the_admin_starts_are_the_jobs_it_may_start():
    control = (TF / "admin_control.tf").read_text()
    block = re.search(r"admin_jobs = toset\(\[(.*?)\]\)", control, re.S).group(1)
    granted = set(re.findall(r'"([a-z0-9-]+)"', block))
    assert granted == set(jobs.JOB_NAMES)
    ingestion = (TF / "ingestion.tf").read_text()
    defined = set(re.findall(r'^\s+"([a-z0-9-]+)"\s+=\s+\{', ingestion, re.M))
    assert granted <= defined, granted - defined
    assert "discover" not in granted and "probe" not in granted
    # Pull now's job: the places the owner flagged, by name (pull --only), never the registry's whole probe.
    pulls = [s for a in jobs.ACTIONS.values() for s in a.steps(jobs._SAMPLE) if s.job == "pull"]
    assert pulls and all(
        s.args and s.args[0].startswith("rc=0; coffeedata pull --only a.example ") for s in pulls
    )
    # The job's whole block, one line or several (`"pull" = { ... }`), with Terraform's alignment spaces collapsed.
    job = re.search(r'^\s+"pull"\s+=\s+\{(.*?)\}\s*$', ingestion, re.M | re.S)
    assert job, "no pull job in ingestion.tf"
    block = " ".join(job.group(1).split())
    assert 'args = ["coffeedata pull --help"]' in block and "retries = 0" in block


def test_the_publish_job_does_nothing_without_the_admins_arguments():
    ingestion = (TF / "ingestion.tf").read_text()
    line = next(x for x in ingestion.splitlines() if x.strip().startswith('"publish"'))
    assert 'args = ["publish-release", "--help"]' in line and "retries = 0" in line


def test_the_main_root_grants_the_admin_no_project_iam():
    """The admin account's Cloud SQL project grants (client, instanceUser) are the bootstrap root's, applied by the
    owner: the GitHub pipeline, which applies this root, never writes project IAM for it."""
    for name in ("admin.tf", "admin_control.tf"):
        code = "\n".join(line.split("#", 1)[0] for line in (TF / name).read_text().splitlines())
        assert 'resource "google_project_iam_member"' not in code, name
        assert "google_project_iam_member." not in code, name


# --- admin_enabled and the control plane (admin.tf's switch; tests/test_admin.py has the service's side) -----------


def _create(address: str, rtype: str, after: dict, unknown: dict | None = None) -> dict:
    return {
        "address": address,
        "type": rtype,
        "name": address.split(".")[1].split("[")[0],
        "mode": "managed",
        "change": {"actions": ["create"], "before": None, "after": after, "after_unknown": unknown or {}},
    }


# What `terraform plan` shows for admin_control.tf with admin_enabled off: everything but the alert on every write,
# which names the service. Invented values; what Terraform learns only at apply is unknown, as in a real plan.
CONTROL_PLAN_OFF = [
    _create("google_project_service.admin_sqladmin", "google_project_service",
            {"service": "sqladmin.googleapis.com", "disable_on_destroy": False}),
    _create("google_sql_database_instance.admin", "google_sql_database_instance",
            {"name": "registry-coffee-admin", "database_version": "POSTGRES_16", "deletion_protection": True,
             "clone": [], "restore_backup_context": [],
             "settings": [{"edition": "ENTERPRISE", "tier": "db-f1-micro",
                           "database_flags": [{"name": "cloudsql.iam_authentication", "value": "on"}],
                           "ip_configuration": [{"ipv4_enabled": True, "authorized_networks": [],
                                                 "ssl_mode": "TRUSTED_CLIENT_CERTIFICATE_REQUIRED"}]}]},
            {"master_instance_name": True, "instance_type": True, "connection_name": True}),
    _create("google_sql_database.admin", "google_sql_database",
            {"name": "admin", "instance": "registry-coffee-admin"}),
    _create("google_sql_user.admin", "google_sql_user",
            {"type": "CLOUD_IAM_SERVICE_ACCOUNT", "instance": "registry-coffee-admin", "password": None,
             "password_policy": []}, {"name": True}),
    _create("google_storage_bucket.admin_snapshots", "google_storage_bucket",
            {"name": "coffee-data-tracker-admin-snapshots", "location": "US-CENTRAL1", "force_destroy": False,
             "uniform_bucket_level_access": True, "public_access_prevention": "enforced"}),
    *[_create(f'google_storage_bucket_iam_member.admin_snapshots["{k}"]', "google_storage_bucket_iam_member",
              {"bucket": "coffee-data-tracker-admin-snapshots", "role": role}, {"member": True})
      for k, role in (("creator", "roles/storage.objectCreator"), ("viewer", "roles/storage.objectViewer"))],
    _create("google_storage_bucket_iam_member.ingest_snapshots", "google_storage_bucket_iam_member",
            {"bucket": "coffee-data-tracker-admin-snapshots", "role": "roles/storage.objectViewer"},
            {"member": True}),
    *[_create(f'google_cloud_run_v2_job_iam_member.admin["{job}-{k}"]', "google_cloud_run_v2_job_iam_member",
              {"location": "us-central1", "role": role}, {"member": True, "name": True})
      for job in sorted(jobs.JOB_NAMES)
      for k, role in (("executor", "roles/run.jobsExecutorWithOverrides"), ("viewer", "roles/run.viewer"))],
]  # fmt: skip
# The one more that the PR turning admin_enabled on adds from this file.
CONTROL_PLAN_SWITCH = [
    _create("google_monitoring_alert_policy.admin_writes[0]", "google_monitoring_alert_policy",
            {"display_name": "admin.registry.coffee: a write", "combiner": "OR",
             "conditions": [{"condition_matched_log": [
                 {"filter": 'resource.type="cloud_run_revision" AND '
                            'resource.labels.service_name="registry-coffee-admin" AND '
                            'jsonPayload.event="admin_write"'}]}]},
            {"notification_channels": True, "name": True}),
]  # fmt: skip
# The instance's configuration block, as the plan JSON carries it (a later guard reads it for a replica's source).
CONTROL_CONFIGURATION = {
    "root_module": {
        "resources": [
            {
                "address": "google_sql_database_instance.admin",
                "type": "google_sql_database_instance",
                "expressions": {"name": {}, "database_version": {}, "settings": [{}]},
            }
        ]
    }
}


def _code(name: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in (TF / name).read_text().splitlines())


def test_only_the_write_alert_waits_for_admin_enabled_in_the_control_plane():
    """The alert on every write names the service, so it is made with it. The Cloud SQL instance, its database and
    user, the snapshot bucket and the job grants need no secret value: the first apply, with the switch off, makes
    them. Nothing in admin_control.tf reads the service itself."""
    code = _code("admin_control.tf")
    declared = set(re.findall(r'^resource "(\w+)" "(\w+)"', code, re.M))
    gated = set(re.findall(r'^resource "(\w+)" "(\w+)" \{\n  count = local\.admin_count\n', code, re.M))
    assert gated == {("google_monitoring_alert_policy", "admin_writes")}
    assert declared == {(rc["type"], rc["name"]) for rc in CONTROL_PLAN_OFF + CONTROL_PLAN_SWITCH}
    assert {(rc["type"], rc["name"]) for rc in CONTROL_PLAN_SWITCH} == gated
    assert "google_cloud_run_v2_service.admin" not in code
    assert 'service_name=\\"${local.admin_service}\\"' in code
    # The service's own references to the control plane are to resources that always exist.
    service = _code("admin.tf").split('resource "google_cloud_run_v2_service" "admin"', 1)[1]
    service = service.split("\nresource ", 1)[0]
    for ref in re.findall(r"google_\w+\.\w+(?:\[\w+\])?", service):
        assert not ref.startswith("google_monitoring_alert_policy"), ref


@pytest.mark.parametrize("state", ["off", "switched-on", "on-from-the-start"])
def test_the_plan_guard_passes_the_whole_admin_plan_with_the_switch_off_and_on(state):
    """scripts/tf_plan_guard.py over admin.tf's and admin_control.tf's plans together, added to the guard's own good
    plan, in each state: the first apply (off), the PR that turns the switch on, and both at once."""
    from tests.test_admin import ADMIN_PLAN_OFF, ADMIN_PLAN_SWITCH, plan_guard_results

    off, switch = ADMIN_PLAN_OFF + CONTROL_PLAN_OFF, ADMIN_PLAN_SWITCH + CONTROL_PLAN_SWITCH
    changes = {"off": off, "switched-on": switch, "on-from-the-start": off + switch}[state]
    results = plan_guard_results(changes, CONTROL_CONFIGURATION)
    assert results and all(bad == [] for bad in results.values()), results
