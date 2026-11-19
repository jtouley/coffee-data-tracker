"""The bootstrap's custom roles hold exactly these permissions: a change to one is a change to what GitHub's plan or
apply account may do, so it shows up here and in review, not only in a plan.

The apply account reads (it holds no push, tag or delete permission) the image repository's images: Cloud Run
resolves a job's or the usage service's image as the account that deploys it, and without `downloadArtifacts` every
`google_cloud_run_v2_job` update failed with `Docker-GetManifest permission_denied`. Cloud Run's "Create jobs" page
asks the deployer for Artifact Registry Reader on the image's repository: `tfApplyRepository` holds that role's
repository-scoped read permissions, on that one repository only.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/terraform/bootstrap"
IAM = ROOT / "iam.tf"
ROLE = re.compile(r'role_id\s*=\s*"(\w+)"[^}]*?permissions\s*=\s*\[([^\]]*)\]', re.S)
RESOURCE = re.compile(r'^resource "(\w+)" "(\w+)" \{\n(.*?)^\}', re.M | re.S)

# roles/artifactregistry.reader's permissions at repository scope (IAM roles reference, Artifact Registry), less
# repositories.list (checked on the location), exportArtifacts and the Maven, npm and Python reads; plus the repository's settings and IAM. Nothing that writes
# an image, a tag, a file or a package, or deletes anything.
REPOSITORY_READ = [
    "artifactregistry.attachments.get",
    "artifactregistry.attachments.list",
    "artifactregistry.dockerimages.get",
    "artifactregistry.dockerimages.list",
    "artifactregistry.files.download",
    "artifactregistry.files.get",
    "artifactregistry.files.list",
    "artifactregistry.packages.get",
    "artifactregistry.packages.list",
    "artifactregistry.repositories.downloadArtifacts",
    "artifactregistry.repositories.get",
    "artifactregistry.repositories.listEffectiveTags",
    "artifactregistry.repositories.listTagBindings",
    "artifactregistry.repositories.readViaVirtualRepository",
    "artifactregistry.rules.get",
    "artifactregistry.rules.list",
    "artifactregistry.tags.get",
    "artifactregistry.tags.list",
    "artifactregistry.versions.get",
    "artifactregistry.versions.list",
]
REPOSITORY_SETTINGS = [
    "artifactregistry.repositories.getIamPolicy",
    "artifactregistry.repositories.setIamPolicy",
    "artifactregistry.repositories.update",
]


def _roles() -> dict[str, list[str]]:
    code = IAM.read_text()
    roles = {rid: sorted(re.findall(r'"([^"]+)"', body)) for rid, body in ROLE.findall(code)}
    declared = len(re.findall(r'^resource "google_project_iam_custom_role"', code, re.M))
    # A role the pattern can't read (permissions before role_id, a non-literal list) must fail, not be skipped.
    assert len(roles) == declared, f"parsed {sorted(roles)} of {declared} custom roles"
    return roles


def _resources() -> list[tuple[str, str, str]]:
    """Every resource in the bootstrap root: (type, name, body without comments)."""
    out = []
    for tf in sorted(ROOT.glob("*.tf")):
        for kind, name, body in RESOURCE.findall(tf.read_text()):
            out.append((kind, name, "\n".join(line.split("#")[0] for line in body.splitlines())))
    return out


def test_every_custom_role_holds_exactly_its_permissions():
    assert _roles() == {
        "tfPlanBucketReader": ["storage.buckets.get"],
        "tfApplyBucketAdmin": ["storage.buckets.get", "storage.buckets.getIamPolicy", "storage.buckets.setIamPolicy",
                               "storage.buckets.update"],
        "tfApplyServiceAccounts": ["iam.serviceAccounts.create", "iam.serviceAccounts.get", "iam.serviceAccounts.list",
                                   "iam.serviceAccounts.update"],
        "tfApplyRepository": sorted(REPOSITORY_READ + REPOSITORY_SETTINGS),
        "tfApplyPlanObject": ["storage.objects.delete", "storage.objects.get"],
        "tfApplySecrets": ["secretmanager.secrets.create", "secretmanager.secrets.get",
                           "secretmanager.secrets.getIamPolicy", "secretmanager.secrets.list",
                           "secretmanager.secrets.setIamPolicy", "secretmanager.secrets.update"],
        "tfApplyLogExclusions": ["logging.exclusions.create", "logging.exclusions.get", "logging.exclusions.list",
                                 "logging.exclusions.update", "logging.notificationRules.create",
                                 "logging.notificationRules.get", "logging.notificationRules.list",
                                 "logging.notificationRules.update"],
        "tfPlanLogExclusions": ["logging.exclusions.get", "logging.exclusions.list"],
        "tfPlanBudgetReader": ["billing.resourcebudgets.read", "resourcemanager.projects.get"],
    }  # fmt: skip


def test_only_the_apply_account_writes_logging_notification_rules_and_never_deletes_one():
    """A log-based alert policy (condition_matched_log) creates a Logging notification rule underneath: CreateAlertPolicy
    failed for the apply account with `logging.notificationRules.create` denied. It holds create, get, list and update
    in `tfApplyLogExclusions`, and no delete (the guard refuses deletes). The plan account reads alert policies through
    roles/monitoring.viewer and needs no notification-rule permission."""
    held = {
        rid: [p for p in perms if p.startswith("logging.notificationRules.")]
        for rid, perms in _roles().items()
    }
    assert {rid: perms for rid, perms in held.items() if perms} == {
        "tfApplyLogExclusions": ["logging.notificationRules.create", "logging.notificationRules.get",
                                 "logging.notificationRules.list", "logging.notificationRules.update"],
    }  # fmt: skip
    granted = {p for perms in _roles().values() for p in perms}
    assert "logging.notificationRules.delete" not in granted and "logging.notificationRules.*" not in granted


def test_no_custom_role_holds_an_artifact_registry_permission_outside_the_allowlist():
    allowed = set(REPOSITORY_READ + REPOSITORY_SETTINGS)
    held = {p for perms in _roles().values() for p in perms if p.startswith("artifactregistry.")}
    assert held <= allowed, sorted(held - allowed)


def test_no_project_level_grant_of_an_artifact_registry_role_or_the_repository_role():
    """Project-wide, the repository role would reach every repository: not in tf_apply, tf_apply_custom or any other."""
    project = [(name, body) for kind, name, body in _resources() if kind == "google_project_iam_member"]
    assert {name for name, _ in project} >= {"tf_plan", "tf_plan_custom", "tf_apply", "tf_apply_custom"}
    for name, body in project:
        assert "artifactregistry" not in body, name
        assert "tf_repository" not in body, name


def test_each_account_has_exactly_one_artifact_registry_grant_on_the_one_repository():
    """Any other Artifact Registry role (writer, repoAdmin, admin) to the plan or apply account, at any scope, fails."""
    registry = []
    for kind, name, body in _resources():
        is_iam = re.search(r"_iam_(member|binding|policy)$", kind)
        if kind.startswith("google_artifact_registry") or (
            is_iam and re.search(r"artifactregistry|tf_repository", body)
        ):
            registry.append((kind, name, body))
    assert [(kind, name) for kind, name, _ in registry] == [
        ("google_artifact_registry_repository_iam_member", "tf_plan"),
        ("google_artifact_registry_repository_iam_member", "tf_apply"),
    ]
    plan, apply = (body for _, _, body in registry)
    for body in (plan, apply):
        assert "repository = local.image_repository" in body
    assert 'role       = "roles/artifactregistry.reader"' in plan and "member     = local.plan_member" in plan
    assert "role       = google_project_iam_custom_role.tf_repository.id" in apply
    assert "member     = local.apply_member" in apply
    # The repository role is bound nowhere else.
    code = "".join(tf.read_text() for tf in ROOT.glob("*.tf"))
    assert code.count("google_project_iam_custom_role.tf_repository") == 1
