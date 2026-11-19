"""The admin's job list and its Terraform grants agree: admin/jobs.py JOB_NAMES (every job an action can start) is
exactly local.admin_jobs in src/terraform/admin_control.tf (the jobs the admin's account may run with overrides),
and each is a job in local.ingest_jobs. A job added to one and not the other fails here, not in production."""

import re
from pathlib import Path

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
