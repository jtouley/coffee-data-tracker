"""The bootstrap's custom roles hold exactly these permissions: a change to one is a change to what GitHub's plan or
apply account may do, so it shows up here and in review, not only in a plan.

The apply account reads (it holds no push, tag or delete permission) the image repository's images: Cloud Run
resolves a job's or the usage service's image as the account that deploys it, and without `downloadArtifacts` every
`google_cloud_run_v2_job` update failed with `Docker-GetManifest permission_denied`. Cloud Run's "Create jobs" page
asks the deployer for Artifact Registry Reader on the image's repository: `tfApplyRepository` holds that role's
repository-scoped read permissions, on that one repository only.

The admin (admin.tf, admin_control.tf on the admin stack) adds a service account the apply account acts as, two buckets
made by name, a Cloud SQL instance, database and IAM user, and two project grants of Cloud SQL roles: the grants below
cover those and nothing wider.
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
# tfApplyRegistry's, on the project: create a repository, list them, change their settings. Not setIamPolicy.
REGISTRY_PROJECT = [
    "artifactregistry.repositories.create",
    "artifactregistry.repositories.list",
    "artifactregistry.repositories.update",
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
                                 "logging.exclusions.update", "logging.logMetrics.create", "logging.logMetrics.get",
                                 "logging.logMetrics.list", "logging.logMetrics.update",
                                 "logging.notificationRules.create",
                                 "logging.notificationRules.get", "logging.notificationRules.list",
                                 "logging.notificationRules.update"],
        "tfPlanLogExclusions": ["logging.exclusions.get", "logging.exclusions.list"],
        "tfPlanBudgetReader": ["billing.resourcebudgets.read", "resourcemanager.projects.get"],
        "tfApplyBucketCreate": ["storage.buckets.create"],
        "tfApplyCloudSql": sorted(f"cloudsql.{kind}.{verb}" for kind in ("databases", "instances", "users")
                                  for verb in ("create", "get", "list", "update")),
        "tfApplyMonitoring": sorted(f"monitoring.{kind}.{verb}"
                                    for kind in ("dashboards", "groups", "services", "slos", "snoozes")
                                    for verb in ("create", "get", "list", "update")),
        "tfApplyRegistry": sorted(REPOSITORY_READ + REGISTRY_PROJECT),
        "tfPlanReader": ["artifactregistry.repositories.get", "artifactregistry.repositories.list",
                         "logging.logMetrics.get", "logging.logMetrics.list"],
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
    allowed = set(REPOSITORY_READ + REPOSITORY_SETTINGS + REGISTRY_PROJECT)
    held = {p for perms in _roles().values() for p in perms if p.startswith("artifactregistry.")}
    assert held <= allowed, sorted(held - allowed)


def test_no_project_level_grant_of_an_artifact_registry_role_or_the_repository_role():
    """Project-wide, the repository role (it holds setIamPolicy) would reach every repository: not in tf_apply,
    tf_apply_custom or any other. tfApplyRegistry is project-wide by design and holds no setIamPolicy."""
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


def _resource(kind: str, name: str) -> str:
    found = [body for k, n, body in _resources() if (k, n) == (kind, name)]
    assert len(found) == 1, (kind, name)
    return found[0]


def test_no_custom_role_holds_a_cloud_sql_delete_export_import_connect_or_login():
    """The apply account creates, reads, lists and updates Cloud SQL instances, databases and users; it never deletes,
    exports, imports, clones, connects or logs in (roles/cloudsql.admin would). Unconditioned: Cloud SQL's IAM
    Conditions don't support users or databases, and document none for instance creation. The guard's cloud-sql-access
    bounds what a plan can do with it."""
    held = {p for perms in _roles().values() for p in perms if p.startswith("cloudsql.")}
    for p in held:
        assert p.rsplit(".", 1)[1] in {"create", "get", "list", "update"}, p
    code = "".join(tf.read_text() for tf in ROOT.glob("*.tf"))
    assert '"roles/cloudsql.admin"' not in code and '"roles/cloudsql.editor"' not in code
    apply = _resource("google_project_iam_member", "tf_apply_custom")
    assert "cloud_sql        = google_project_iam_custom_role.tf_cloud_sql.id" in apply


def test_the_pipeline_never_writes_project_iam():
    """No custom role holds the project's setIamPolicy, and no predefined role that does is granted: the admin
    account's two Cloud SQL project grants are this root's, applied by the owner (admin_sql)."""
    assert not [
        rid for rid, perms in _roles().items() if any("setIamPolicy" in p and "projects" in p for p in perms)
    ]
    code = "".join(tf.read_text() for tf in ROOT.glob("*.tf"))
    for role in (
        "roles/resourcemanager.projectIamAdmin",
        "roles/owner",
        "roles/editor",
        "roles/iam.securityAdmin",
    ):
        assert f'"{role}"' not in code, role
    assert "modifiedGrantsByRole" not in code


def test_the_admins_cloud_sql_grants_are_the_owners_with_the_admin_stacks_condition():
    """admin_sql here: the same keys, roles, member and condition as admin_control.tf's (#128/#149) had in the main
    root, written statically (the account and instance names are fixed)."""
    body = _resource("google_project_iam_member", "admin_sql")
    assert (
        'for_each = { client = "roles/cloudsql.client", instance_user = "roles/cloudsql.instanceUser" }'
        in body
    )
    assert (
        'member   = "serviceAccount:${var.service}-admin-run@${var.project_id}.iam.gserviceaccount.com"'
        in body
    )
    assert 'title       = "only the admin\'s instance"' in body
    assert 'description = "The admin\'s control-plane database, nothing else."' in body
    assert (
        'expression  = "resource.name == \\"projects/${var.project_id}/instances/${var.service}-admin\\""'
        in body
    )


def test_buckets_are_reached_by_the_projects_prefix_never_the_state_or_plans_bucket():
    """New buckets don't exist when the bootstrap applies, so their settings and IAM come from project grants
    conditioned on the "<project>-" prefix, minus the state and plans buckets; creation (checked on the project) is a
    role of its own."""
    code = "".join(tf.read_text() for tf in ROOT.glob("*.tf"))
    for clause in (
        "\"resource.type == 'storage.googleapis.com/Bucket'\"",
        "\"resource.name.startsWith('projects/_/buckets/${var.project_id}-')\"",
        "\"resource.name != 'projects/_/buckets/${var.state_bucket}'\"",
        "\"resource.name != 'projects/_/buckets/${google_storage_bucket.tf_plans.name}'\"",
    ):
        assert clause in code, clause
    assert 'managed_buckets_condition = join(" && ", [' in code
    for name, role, member in (
        ("tf_apply_buckets", "tf_bucket_admin", "apply"),
        ("tf_plan_buckets", "tf_bucket_reader", "plan"),
    ):
        body = _resource("google_project_iam_member", name)
        assert f"role    = google_project_iam_custom_role.{role}.id" in body
        assert f"member  = local.{member}_member" in body
        assert "expression = local.managed_buckets_condition" in body
    # Bucket admin or reader at project scope only through those two conditioned grants.
    for kind, name, body in _resources():
        if kind == "google_project_iam_member" and re.search(r"tf_bucket_(admin|reader)\b", body):
            assert name in {"tf_apply_buckets", "tf_plan_buckets"}, name
    apply = _resource("google_project_iam_member", "tf_apply_custom")
    assert "bucket_create    = google_project_iam_custom_role.tf_bucket_create.id" in apply


# What the apply account held before the bounded set widened (main at d5d705e and since): the hard line is measured
# against it.
TODAY_APPLY_ROLES = {
    "roles/browser",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/iam.workloadIdentityPoolViewer",
    "roles/iam.securityReviewer",
    "roles/run.admin",
    "roles/compute.networkAdmin",
    "roles/cloudscheduler.admin",
    "roles/monitoring.alertPolicyEditor",
    "roles/monitoring.notificationChannelEditor",
    "roles/monitoring.uptimeCheckConfigEditor",
    "roles/iam.serviceAccountUser",  # tf_apply_acts_as, on the managed accounts only
    "roles/billing.costsManager",  # the budget, on the billing account
    "roles/storage.objectAdmin",  # the main root's state objects only (a condition)
}
TODAY_SET_IAM_POLICY_ROLES = {"tfApplyBucketAdmin", "tfApplyRepository", "tfApplySecrets"}
TODAY_DELETES = {"storage.objects.delete"}  # tfApplyPlanObject: the saved plan, after the apply
TODAY_SERVICE_ACCOUNT_PERMS = {f"iam.serviceAccounts.{v}" for v in ("create", "get", "list", "update")}


def _apply_grants() -> list[tuple[str, str, str]]:
    """Every grant to the apply account: (type, name, role expression)."""
    out = []
    for kind, name, body in _resources():
        if kind.endswith("_iam_member") and "member" in body and "local.apply_member" in body:
            role = re.search(r"^\s*role\s*=\s*(.+)$", body, re.M).group(1).strip()
            out.append((kind, name, role))
    return out


def test_the_hard_line_holds():
    """The bounded set widened to create, read, list and update the ordinary types of every service in use, and
    nothing past the hard line: no project or service-account setIamPolicy, no iam.roles.*, no service-account
    permission beyond today's, no keys, no workload identity pools, nothing on billing beyond the budget, nothing on
    the state or plans buckets' IAM, no basic role, no predefined admin role beyond today's, no delete beyond today's,
    setIamPolicy only in the roles that held it today."""
    roles = _roles()
    perms = {p for ps in roles.values() for p in ps}
    forbidden_prefixes = (
        "resourcemanager.projects.setIamPolicy",
        "resourcemanager.projects.update",
        "iam.roles.",
        "iam.serviceAccountKeys.",
        "iam.workloadIdentityPool",
        "iam.denypolicies.",
        "orgpolicy.",
    )
    assert not [p for p in perms if p.startswith(forbidden_prefixes)]
    assert {p for p in perms if p.startswith("iam.serviceAccounts.")} <= TODAY_SERVICE_ACCOUNT_PERMS
    assert {p for p in perms if p.startswith("billing.")} == {"billing.resourcebudgets.read"}
    assert roles["tfPlanBudgetReader"] == ["billing.resourcebudgets.read", "resourcemanager.projects.get"]
    assert {p for p in perms if p.endswith(".delete")} <= TODAY_DELETES
    assert {rid for rid, ps in roles.items() if any(p.endswith(".setIamPolicy") for p in ps)} == (
        TODAY_SET_IAM_POLICY_ROLES
    )
    assert not [p for p in perms if "*" in p]

    # Predefined roles: none basic, no admin role the account didn't hold today, nothing new but the custom roles.
    code = "\n".join(body for _, _, body in _resources())
    named = set(re.findall(r'"(roles/[A-Za-z.]+)"', code))
    assert not named & {"roles/owner", "roles/editor", "roles/viewer"}
    apply_predefined = set()
    for kind, name, role in _apply_grants():
        if role.startswith("each.value"):
            body = _resource(kind, name)
            apply_predefined |= set(re.findall(r'"(roles/[A-Za-z.]+)"', body))
        elif role.startswith('"roles/'):
            apply_predefined.add(role.strip('"').split('"')[0])
        else:
            assert role.startswith("google_project_iam_custom_role."), (name, role)
    assert apply_predefined <= TODAY_APPLY_ROLES, sorted(apply_predefined - TODAY_APPLY_ROLES)
    admin = {r for r in named if re.search(r"admin", r, re.I)}
    assert admin <= {r for r in TODAY_APPLY_ROLES if re.search(r"admin", r, re.I)}, sorted(admin)

    # Service-account IAM: only the apply account acting as the main root's runtime accounts, as today.
    sa = [(n, r) for k, n, r in _apply_grants() if k == "google_service_account_iam_member"]
    assert sa == [("tf_apply_acts_as", '"roles/iam.serviceAccountUser"')]

    # Billing: the budget role on the billing account, nothing else, as today.
    billing = [(k, n) for k, n, _ in _resources() if k.startswith("google_billing")]
    assert billing == [("google_billing_account_iam_member", "tf_apply")]

    # The state and plans buckets: the four grants of today, none of a role holding setIamPolicy; the managed-bucket
    # condition excludes both.
    on_state_or_plans = sorted(
        n
        for k, n, b in _resources()
        if k == "google_storage_bucket_iam_member"
        and ("var.state_bucket" in b or "google_storage_bucket.tf_plans" in b)
    )
    assert on_state_or_plans == ["tf_apply_plans", "tf_apply_state", "tf_plan_plans", "tf_plan_state"]
    for name in on_state_or_plans:
        body = _resource("google_storage_bucket_iam_member", name)
        assert "tf_bucket_admin" not in body and 'Admin"' not in body.replace("objectAdmin", "")
    assert "resource.name != 'projects/_/buckets/${var.state_bucket}'" in code + "".join(
        tf.read_text() for tf in ROOT.glob("*.tf")
    )


def test_the_apply_account_acts_as_the_admins_account_and_no_new_other():
    code = "".join(tf.read_text() for tf in ROOT.glob("*.tf"))
    assert 'admin            = "${var.service}-admin-run"' in code
    body = _resource("google_service_account_iam_member", "tf_apply_acts_as")
    assert 'for_each           = toset(["runtime", "ingest", "ingest_scheduler", "usage", "admin"])' in body
    assert 'role               = "roles/iam.serviceAccountUser"' in body
