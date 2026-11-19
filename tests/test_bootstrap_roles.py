"""The bootstrap's custom roles hold exactly these permissions: a change to one is a change to what GitHub's plan or
apply account may do, so it shows up here and in review, not only in a plan.

The apply account reads (never pushes, tags or deletes) the image repository's images: Cloud Run resolves a job's
image manifest as the account that deploys it, and without `downloadArtifacts` every `google_cloud_run_v2_job`
update fails with `Docker-GetManifest permission_denied`.
"""

import re
from pathlib import Path

IAM = Path(__file__).resolve().parents[1] / "src/terraform/bootstrap/iam.tf"
ROLE = re.compile(r'role_id\s*=\s*"(\w+)"[^}]*?permissions\s*=\s*\[([^\]]*)\]', re.S)


def _roles() -> dict[str, list[str]]:
    return {rid: sorted(re.findall(r'"([^"]+)"', body)) for rid, body in ROLE.findall(IAM.read_text())}


def _block(code: str, header: str) -> str:
    start = code.index(header)
    return code[start : code.index("\n}", start)]


def test_every_custom_role_holds_exactly_its_permissions():
    assert _roles() == {
        "tfPlanBucketReader": ["storage.buckets.get"],
        "tfApplyBucketAdmin": ["storage.buckets.get", "storage.buckets.getIamPolicy", "storage.buckets.setIamPolicy",
                               "storage.buckets.update"],
        "tfApplyServiceAccounts": ["iam.serviceAccounts.create", "iam.serviceAccounts.get", "iam.serviceAccounts.list",
                                   "iam.serviceAccounts.update"],
        "tfApplyRepository": ["artifactregistry.repositories.downloadArtifacts", "artifactregistry.repositories.get",
                              "artifactregistry.repositories.getIamPolicy", "artifactregistry.repositories.setIamPolicy",
                              "artifactregistry.repositories.update"],
        "tfApplyPlanObject": ["storage.objects.delete", "storage.objects.get"],
        "tfApplySecrets": ["secretmanager.secrets.create", "secretmanager.secrets.get",
                           "secretmanager.secrets.getIamPolicy", "secretmanager.secrets.list",
                           "secretmanager.secrets.setIamPolicy", "secretmanager.secrets.update"],
        "tfApplyLogExclusions": ["logging.exclusions.create", "logging.exclusions.get", "logging.exclusions.list",
                                 "logging.exclusions.update"],
        "tfPlanLogExclusions": ["logging.exclusions.get", "logging.exclusions.list"],
        "tfPlanBudgetReader": ["billing.resourcebudgets.read", "resourcemanager.projects.get"],
    }  # fmt: skip


def test_the_apply_account_reads_images_on_the_image_repository_only():
    code = IAM.read_text()
    # Granted on the one repository, not the project: no project-level Artifact Registry role for either account.
    grant = _block(code, 'resource "google_artifact_registry_repository_iam_member" "tf_apply"')
    assert "repository = local.image_repository" in grant
    assert "role       = google_project_iam_custom_role.tf_repository.id" in grant
    assert "member     = local.apply_member" in grant
    project_roles = _block(code, 'resource "google_project_iam_member" "tf_apply"')
    project_roles += _block(code, 'resource "google_project_iam_member" "tf_plan"')
    assert "artifactregistry" not in project_roles
    # Read only: nothing that writes, tags or deletes an image, in any custom role.
    perms = {p for ps in _roles().values() for p in ps}
    assert not {p for p in perms if re.search(r"uploadArtifacts|deleteArtifacts|\.tags\.|\.versions\.delete", p)}


def test_the_plan_account_already_reads_the_repository_and_never_deploys():
    """roles/artifactregistry.reader includes downloadArtifacts; the plan account deploys nothing, so needs no more."""
    grant = _block(IAM.read_text(), 'resource "google_artifact_registry_repository_iam_member" "tf_plan"')
    assert 'role       = "roles/artifactregistry.reader"' in grant
    assert "member     = local.plan_member" in grant
