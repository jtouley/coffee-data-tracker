"""scripts/tf_plan_guard.py and terraform-plan.yml's own step scripts, on hand-made `terraform show -json` plans.

The fixtures (tests/fixtures/tf_plan/) hold invented values only: one good plan, and one per way a guard must fail.
The workflow tests run the steps' scripts as written, with a stand-in for the Terraform binary on a private PATH
(the real one is never run) that fails with fake secret values in its output.
"""

import base64
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tf_plan_guard.py"
FIXTURES = ROOT / "tests" / "fixtures" / "tf_plan"
WORKFLOW = ROOT / ".github" / "workflows" / "terraform-plan.yml"

_spec = importlib.util.spec_from_file_location("tf_plan_guard", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
sys.modules["tf_plan_guard"] = guard
_spec.loader.exec_module(guard)


def doc(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def plan(d: dict):
    return guard.load(json.dumps(d))


def run(*args, stdin: str | None = None, env: dict | None = None):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=env
    )


def results(d: dict) -> dict[str, list[str]]:
    return {n: bad for n, _, bad in guard.run_guards(plan(d))}


# fixture -> the one guard it must fail, and the addresses it names (None: not compared)
FAILING = {
    # the cutover script's own cases
    "fail_delete_replace": ("no-delete-or-replace", ["google_storage_bucket.ingest_staging"]),
    "fail_shopify_subnet": ("shopify-network-path", ["google_compute_subnetwork.shopify_egress"]),
    "fail_router": ("shopify-network-path", ["google_compute_router.ingest"]),
    "fail_nat_ips": ("shopify-network-path", ["google_compute_router_nat.shopify_egress"]),
    "fail_shopify_job_network": ("shopify-jobs-network", ['google_cloud_run_v2_job.ingest["crawl-retry"]']),
    "fail_image_tag": ("ingest-image-pinned", None),
    # shapes the exact-address checks missed
    "fail_router_moved_into_module": (
        "shopify-network-path",
        ["module.net.google_compute_router.ingest_v2[0]"],
    ),
    "fail_network_in_module": ("shopify-network-path", ["module.vpc.google_compute_network.main"]),
    "fail_nat_moved_new_ips": ("shopify-network-path", ["google_compute_router_nat.primary"]),
    "fail_extra_nat_on_router": ("shopify-network-path", ["google_compute_router_nat.extra"]),
    "fail_woo_nat_takes_shopify_subnet": (
        "shopify-network-path",
        ["google_compute_router_nat.woo_generic_egress"],
    ),
    "fail_image_unknown": ("ingest-image-pinned", ['google_cloud_run_v2_job.ingest["merge"]']),
    "fail_wif_provider": ("workload-identity", ["google_iam_workload_identity_pool_provider.github"]),
    "fail_wif_grant": ("workload-identity", ["google_service_account_iam_member.extra"]),
}
ORIGINAL = ["good", *list(FAILING)[:6]]


def test_good_plan_passes_every_guard():
    assert not any(results(doc("good")).values())
    r = run("check", str(FIXTURES / "good.json"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("PASS") == len(guard.GUARDS)


@pytest.mark.parametrize("name", sorted(FAILING))
def test_each_failing_plan_fails_its_guard_only(name):
    want, addresses = FAILING[name]
    got = results(doc(name))
    bad = got.pop(want)
    assert bad, f"{name} should fail {want}"
    assert not any(got.values()), f"{name} fails other guards too: {got}"
    if addresses is not None:
        assert bad == addresses
    r = run("check", str(FIXTURES / f"{name}.json"))
    assert r.returncode == 1
    assert f"FAIL {want}" in r.stdout


def test_every_fixture_is_listed():
    assert {p.stem for p in FIXTURES.glob("*.json")} == {"good", *FAILING}


def find(d: dict, address: str) -> dict:
    return next(r for r in d["resource_changes"] if r["address"] == address)


def test_nat_port_settings_may_change_but_not_its_subnetwork():
    d = doc("good")
    nat = find(d, "google_compute_router_nat.shopify_egress")
    nat["change"]["after"]["max_ports_per_vm"] = 16384
    nat["change"]["after"]["log_config"] = [{"enable": True, "filter": "ALL"}]
    assert guard.shopify_network_path(plan(d)) == []
    nat["change"]["after"]["subnetwork"][0]["source_ip_ranges_to_nat"] = ["PRIMARY_IP_RANGE"]
    assert guard.shopify_network_path(plan(d)) == ["google_compute_router_nat.shopify_egress"]


def test_creating_the_shopify_nat_counts_as_a_change():
    d = doc("good")
    nat = find(d, "google_compute_router_nat.shopify_egress")
    nat["change"]["actions"], nat["change"]["before"] = ["create"], None
    assert guard.shopify_network_path(plan(d)) == ["google_compute_router_nat.shopify_egress"]


def test_a_nat_on_another_router_may_be_created():
    d = doc("good")
    d["resource_changes"].append(
        {
            "address": "google_compute_router_nat.elsewhere",
            "type": "google_compute_router_nat",
            "change": {
                "actions": ["create"],
                "before": None,
                "after": {"router": "another-router", "subnetwork": []},
            },
        }
    )
    assert guard.shopify_network_path(plan(d)) == []


def test_a_nat_whose_subnets_are_unknown_is_protected():
    d = doc("good")
    woo = find(d, "google_compute_router_nat.woo_generic_egress")
    del woo["change"]["after"]["subnetwork"]
    woo["change"]["after_unknown"] = {"subnetwork": True}
    assert guard.shopify_network_path(plan(d)) == ["google_compute_router_nat.woo_generic_egress"]


def test_a_nat_subnetwork_of_an_unknown_shape_fails_closed():
    d = doc("good")
    find(d, "google_compute_router_nat.woo_generic_egress")["change"]["after"]["subnetwork"] = {"name": "x"}
    assert run("check", "-", stdin=json.dumps(d)).returncode == 2


def test_shopify_job_network_compared_like_jq():
    d = doc("good")
    job = find(d, 'google_cloud_run_v2_job.ingest["crawl-shopify"]')
    job["change"]["after"]["template"][0]["template"][0]["vpc_access"] = []
    assert guard.shopify_jobs_network(plan(d)) == ['google_cloud_run_v2_job.ingest["crawl-shopify"]']
    job["change"]["before"] = (
        None  # a new job isn't one of Shopify's existing jobs: the jq check skips it too
    )
    assert guard.shopify_jobs_network(plan(d)) == []


def test_forget_counts_as_a_delete():
    d = doc("good")
    find(d, "google_monitoring_alert_policy.job_failed")["change"]["actions"] = ["forget"]
    assert guard.no_delete_or_replace(plan(d)) == ["google_monitoring_alert_policy.job_failed"]


def test_an_iam_grant_with_the_workload_identity_role_fails():
    d = doc("good")
    grant = find(d, "google_artifact_registry_repository_iam_member.deploy")
    grant["change"]["after"]["role"] = "roles/iam.workloadIdentityUser"
    assert guard.workload_identity(plan(d)) == ["google_artifact_registry_repository_iam_member.deploy"]


def _sa_change(d: dict, address: str, rtype: str, after: dict, after_unknown: dict | None = None) -> dict:
    rc = {
        "address": address,
        "type": rtype,
        "change": {
            "actions": ["create"],
            "before": None,
            "after": after,
            "after_unknown": after_unknown or {},
        },
    }
    d["resource_changes"].append(rc)
    return d


DEPLOY = "serviceAccount:registry-coffee-deploy@coffee-data-tracker.iam.gserviceaccount.com"


@pytest.mark.parametrize(
    "rtype, after, after_unknown",
    [
        # who may act as the deploy account
        (
            "google_service_account_iam_member",
            {
                "service_account_id": "projects/coffee-data-tracker/serviceAccounts/"
                "registry-coffee-deploy@coffee-data-tracker.iam.gserviceaccount.com",
                "role": "roles/iam.serviceAccountTokenCreator",
                "member": "user:someone@example.com",
            },
            None,
        ),
        # what the apply account may act as
        (
            "google_service_account_iam_member",
            {
                "service_account_id": "projects/p/serviceAccounts/other@p.iam.gserviceaccount.com",
                "role": "roles/iam.serviceAccountUser",
                "member": "serviceAccount:registry-coffee-tf-apply@p.iam.gserviceaccount.com",
            },
            None,
        ),
        # a project-wide grant to the plan account
        (
            "google_project_iam_member",
            {
                "project": "p",
                "role": "roles/editor",
                "member": "serviceAccount:registry-coffee-tf-plan@p.iam.gserviceaccount.com",
            },
            None,
        ),
        # the deploy account itself
        ("google_service_account", {"account_id": "registry-coffee-deploy", "display_name": "x"}, None),
        # a service account IAM change whose member is known only at apply
        (
            "google_service_account_iam_member",
            {
                "service_account_id": "projects/p/serviceAccounts/other@p.iam.gserviceaccount.com",
                "role": "roles/iam.serviceAccountUser",
            },
            {"member": True},
        ),
        # a project IAM change whose member is known only at apply
        ("google_project_iam_member", {"project": "p", "role": "roles/viewer"}, {"member": True}),
    ],
)
def test_the_guarded_accounts_and_their_trust_are_refused(rtype, after, after_unknown):
    d = _sa_change(doc("good"), f"{rtype}.extra", rtype, after, after_unknown)
    got = results(d)
    assert got.pop("guarded-accounts") == [f"{rtype}.extra"]
    assert not any(got.values())


def test_other_accounts_and_grants_to_the_deploy_account_on_one_resource_pass():
    d = _sa_change(
        doc("good"),
        "google_service_account.ingest_new",
        "google_service_account",
        {"account_id": "registry-coffee-ingest2"},
    )
    _sa_change(
        d,
        "google_cloud_run_v2_service_iam_member.deploy",
        "google_cloud_run_v2_service_iam_member",
        {"role": "roles/run.developer", "member": DEPLOY},
    )
    assert guard.guarded_accounts(plan(d)) == []


NETWORK = "projects/coffee-data-tracker/global/networks/registry-coffee-ingest"


@pytest.mark.parametrize(
    "rtype, after, after_unknown",
    [
        ("google_compute_route", {"name": "r", "network": NETWORK, "dest_range": "0.0.0.0/0"}, None),
        (
            "google_compute_firewall",
            {"name": "f", "network": "registry-coffee-ingest", "direction": "EGRESS"},
            None,
        ),
        ("google_compute_network_peering", {"name": "p", "network": NETWORK, "peer_network": "other"}, None),
        ("google_compute_route", {"name": "r", "dest_range": "0.0.0.0/0"}, {"network": True}),
    ],
)
def test_routes_firewalls_and_peerings_on_the_ingest_network_are_refused(rtype, after, after_unknown):
    d = _sa_change(doc("good"), f"{rtype}.extra", rtype, after, after_unknown)
    got = results(d)
    assert got.pop("shopify-network-path") == [f"{rtype}.extra"]
    assert not any(got.values())


def test_a_route_on_another_network_passes():
    d = _sa_change(
        doc("good"),
        "google_compute_route.other",
        "google_compute_route",
        {"name": "r", "network": "projects/p/global/networks/other"},
    )
    assert guard.shopify_network_path(plan(d)) == []


def test_a_job_image_from_another_registry_fails_even_by_digest():
    d = doc("good")
    job = find(d, 'google_cloud_run_v2_job.ingest["merge"]')
    job["change"]["after"]["template"][0]["template"][0]["containers"][0]["image"] = (
        "evil.example/x@sha256:" + "c" * 64
    )
    assert guard.ingest_image_pinned(plan(d)) == ['google_cloud_run_v2_job.ingest["merge"]']


@pytest.mark.parametrize("acts", [[["create"]], [{"a": 1}], [None]])
def test_malformed_actions_exit_2_with_a_fixed_message(acts):
    d = doc("good")
    d["resource_changes"][0]["change"]["actions"] = acts
    for command in ("check", "render"):
        r = run(command, "-", stdin=json.dumps(d))
        assert r.returncode == 2, (command, r.stdout, r.stderr)
        assert r.stdout == "" and "refusing" in r.stderr


def test_an_unforeseen_error_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(guard, "check_text", lambda plan: 1 / 0)
    monkeypatch.setattr(guard, "_read", lambda path: (FIXTURES / "good.json").read_text())
    assert guard.main(["check", "x"]) == 2
    assert "doesn't understand" in capsys.readouterr().err


def test_output_metadata_is_not_scrubbed_as_a_value():
    d = doc("good")
    d.setdefault("output_changes", {})["new_egress_ip"] = {
        "actions": ["create"],
        "before": None,
        "after": "203.0.113.99",
    }
    scrub = plan(d).scrubber()
    assert (
        scrub.printable("google_monitoring_alert_policy.create_alert")
        == "google_monitoring_alert_policy.create_alert"
    )
    with pytest.raises(guard.Unreadable):
        scrub.printable("google_x.ip_203_0_113_99")


def test_a_nat_subnet_with_an_unknown_name_is_protected_not_unreadable():
    d = doc("good")
    nat = find(d, "google_compute_router_nat.shopify_egress")
    nat["change"]["actions"] = ["update"]
    nat["change"]["after"] = copy.deepcopy(nat["change"]["before"] or nat["change"]["after"])
    nat["change"]["after"]["subnetwork"] = [{"name": None, "source_ip_ranges_to_nat": ["ALL_IP_RANGES"]}]
    assert guard.shopify_network_path(plan(d)) == ["google_compute_router_nat.shopify_egress"]


def test_moves_and_imports_are_listed_and_a_pool_import_is_refused():
    d = doc("good")
    moved = copy.deepcopy(find(d, "google_monitoring_alert_policy.job_failed"))
    moved["address"], moved["previous_address"] = (
        "google_monitoring_alert_policy.renamed",
        "google_monitoring_alert_policy.old",
    )
    moved["change"]["actions"] = ["no-op"]
    pool = {
        "address": "google_iam_workload_identity_pool.imported",
        "type": "google_iam_workload_identity_pool",
        "change": {"actions": ["no-op"], "before": {}, "after": {}, "importing": {"id": "x"}},
    }
    d["resource_changes"] += [moved, pool]
    text = guard.render(plan(d))
    assert (
        "`google_monitoring_alert_policy.renamed` (moved from `google_monitoring_alert_policy.old`)" in text
    )
    assert "`google_iam_workload_identity_pool.imported` (import)" in text
    assert guard.workload_identity(plan(d)) == ["google_iam_workload_identity_pool.imported"]


APPLY_SA = "serviceAccount:registry-coffee-tf-apply@coffee-data-tracker.iam.gserviceaccount.com"


@pytest.mark.parametrize(
    "rtype, after, after_unknown",
    [
        # a key for a guarded account, or for any account: sign-in is keyless
        ("google_service_account_key", {"service_account_id": "registry-coffee-deploy"}, None),
        (
            "google_service_account_key",
            {"service_account_id": "projects/p/serviceAccounts/other@p.iam.gserviceaccount.com"},
            None,
        ),
        # service account IAM naming its account by numeric unique id
        (
            "google_service_account_iam_member",
            {
                "service_account_id": "projects/p/serviceAccounts/104729000000000000001",
                "role": "roles/viewer",
                "member": "user:a@example.com",
            },
            None,
        ),
        # a secret or bucket grant to the apply (or plan) account
        (
            "google_secret_manager_secret_iam_member",
            {"secret_id": "HF_TOKEN", "role": "roles/secretmanager.secretAccessor", "member": APPLY_SA},
            None,
        ),
        (
            "google_storage_bucket_iam_member",
            {
                "bucket": "b",
                "role": "roles/storage.objectViewer",
                "member": APPLY_SA.replace("apply", "plan"),
            },
            None,
        ),
        # a grant to the deploy account beyond its own registry and service roles
        (
            "google_secret_manager_secret_iam_member",
            {"secret_id": "HF_TOKEN", "role": "roles/secretmanager.secretAccessor", "member": DEPLOY},
            None,
        ),
        # project-wide impersonation or key roles, to anyone
        (
            "google_project_iam_member",
            {"project": "p", "role": "roles/iam.serviceAccountTokenCreator", "member": "user:a@example.com"},
            None,
        ),
        (
            "google_project_iam_member",
            {"project": "p", "role": "roles/iam.serviceAccountKeyAdmin", "member": "group:g@example.com"},
            None,
        ),
        (
            "google_project_iam_binding",
            {"project": "p", "role": "roles/iam.serviceAccountUser", "members": ["user:a@example.com"]},
            None,
        ),
        # impersonation of another account, on that account
        (
            "google_service_account_iam_member",
            {
                "service_account_id": "projects/p/serviceAccounts/registry-coffee-run@p.iam.gserviceaccount.com",
                "role": "roles/iam.serviceAccountTokenCreator",
                "member": "user:a@example.com",
            },
            None,
        ),
    ],
)
def test_keys_numeric_ids_grants_and_impersonation_are_refused(rtype, after, after_unknown):
    d = _sa_change(doc("good"), f"{rtype}.extra", rtype, after, after_unknown)
    got = results(d)
    assert got.pop("guarded-accounts") == [f"{rtype}.extra"]
    assert not any(got.values())


def test_a_project_role_unknown_at_plan_is_refused():
    rtype = "google_project_iam_member"
    d = _sa_change(
        doc("good"), f"{rtype}.extra", rtype, {"project": "p", "member": "user:a@example.com"}, {"role": True}
    )
    assert guard.guarded_accounts(plan(d)) == [f"{rtype}.extra"]  # the workload-identity guard refuses it too


def test_ordinary_grants_pass():
    d = _sa_change(
        doc("good"),
        "google_secret_manager_secret_iam_member.run",
        "google_secret_manager_secret_iam_member",
        {
            "secret_id": "HF_TOKEN",
            "role": "roles/secretmanager.secretAccessor",
            "member": "serviceAccount:registry-coffee-run@p.iam.gserviceaccount.com",
        },
    )
    _sa_change(
        d,
        "google_project_iam_member.viewer",
        "google_project_iam_member",
        {"project": "p", "role": "roles/viewer", "member": "user:a@example.com"},
    )
    assert guard.guarded_accounts(plan(d)) == []


@pytest.mark.parametrize(
    "rtype, after",
    [
        (
            "google_compute_router_peer",
            {"name": "bgp", "router": "registry-coffee-ingest", "region": "us-central1"},
        ),
        ("google_compute_router_interface", {"name": "if", "router": "registry-coffee-ingest"}),
        (
            "google_compute_vpn_tunnel",
            {"name": "t", "router": "projects/p/regions/r/routers/registry-coffee-ingest"},
        ),
        ("google_compute_network_firewall_policy_association", {"name": "a", "attachment_target": NETWORK}),
        ("google_vpc_access_connector", {"name": "c", "network": "registry-coffee-ingest"}),
    ],
)
def test_anything_else_on_the_ingest_network_or_router_is_refused(rtype, after):
    d = _sa_change(doc("good"), f"{rtype}.extra", rtype, after)
    got = results(d)
    assert got.pop("shopify-network-path") == [f"{rtype}.extra"]
    assert not any(got.values())


def _service(after_image, before_image="us-docker.pkg.dev/cloudrun/container/hello", actions=("update",)):
    def side(image):
        return {"name": "registry-coffee", "template": [{"containers": [{"image": image}]}]}

    return {
        "address": "google_cloud_run_v2_service.app",
        "type": "google_cloud_run_v2_service",
        "change": {
            "actions": list(actions),
            "before": None if before_image is None else side(before_image),
            "after": side(after_image),
            "after_unknown": {},
        },
    }


APP = "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/app@sha256:" + "d" * 64


@pytest.mark.parametrize(
    "rc, refused",
    [
        (_service(APP), False),
        (_service("us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/app:latest"), True),
        (_service("evil.example/app@sha256:" + "d" * 64), True),
        (
            _service(
                "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/ingest@sha256:" + "d" * 64
            ),
            True,
        ),
        (_service("x", before_image="x"), False),  # unchanged: deploys set the image, not Terraform
        (_service(APP, before_image=None, actions=("create",)), False),
    ],
)
def test_a_service_image_terraform_sets_must_be_the_app_pinned(rc, refused):
    d = doc("good")
    d["resource_changes"].append(rc)
    assert guard.app_image_pinned(plan(d)) == (["google_cloud_run_v2_service.app"] if refused else [])


def test_render_says_what_changes_never_values():
    d = doc("good")
    rc = _service(APP)
    rc["change"]["before"]["labels"] = {"team": "a"}
    rc["change"]["after"]["labels"] = {"team": "b", "SECRET_KEY_NAME": "x"}
    d["resource_changes"].append(rc)
    text = guard.render(plan(d))
    line = next(x for x in text.splitlines() if "google_cloud_run_v2_service.app" in x)
    assert "template.containers.image" in line and "labels.team" in line
    assert "SECRET_KEY_NAME" not in line and "1 more not shown" in line
    assert APP not in text and "evil" not in text


def test_true_is_not_one():
    assert not guard.jq_equal(True, 1)
    assert guard.jq_equal(1, 1.0)
    assert guard.jq_equal({"a": [1, None]}, {"a": [1.0, None]})


@pytest.mark.parametrize(
    "image",
    [
        "",
        "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/ingest:latest",
        "us-central1-docker.pkg.dev/other-project/registry-coffee/ingest@sha256:" + "c" * 64,
        "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/app@sha256:" + "c" * 64,
        "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/ingest@sha256:" + "C" * 64,
        "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/ingest@sha256:" + "c" * 63,
        "FILL_IN",
    ],
)
def test_image_must_be_the_ingest_repository_by_digest(image):
    d = doc("good")
    d["variables"]["ingest_image"]["value"] = image
    assert "var.ingest_image" in guard.ingest_image_pinned(plan(d))


def test_missing_variables_fail_closed():
    d = doc("good")
    del d["variables"]["project_id"]
    bad = guard.ingest_image_pinned(plan(d))
    assert bad[0] == "var.region"  # and, with no valid pin, every job after it
    assert all(b.startswith("google_cloud_run_v2_job.") for b in bad[1:])
    del d["variables"]["service"]
    assert run("check", "-", stdin=json.dumps(d)).returncode == 2


def _good_with(**changes) -> str:
    d = doc("good")
    d.update(changes)
    return json.dumps({k: v for k, v in d.items() if v is not ...})


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        _good_with(format_version=...),
        _good_with(format_version="2.0"),
        _good_with(errored=True),
        _good_with(complete=False),
        _good_with(resource_changes=None),
        _good_with(resource_changes=...),
        _good_with(resource_changes={"a": 1}),
        _good_with(variables=...),
        _good_with(resource_changes=[{"address": "a.b", "type": "a", "change": {}}]),
        _good_with(resource_changes=[{"address": "a.b", "type": "a", "change": {"actions": ["destroy"]}}]),
    ],
)
def test_unreadable_input_fails_closed(text):
    for command in ("check", "render"):
        r = run(command, "-", stdin=text)
        assert r.returncode == 2, (command, text[:80], r.stdout)
        assert r.stdout == ""


def test_wrong_shape_inside_a_change_fails_closed():
    d = doc("good")
    find(d, 'google_cloud_run_v2_job.ingest["crawl-shopify"]')["change"]["before"]["template"] = {
        "not": "a list"
    }
    assert run("check", "-", stdin=json.dumps(d)).returncode == 2


def test_missing_file_and_bad_usage():
    assert run("check", str(FIXTURES / "nope.json")).returncode == 2
    assert run("plan", str(FIXTURES / "good.json")).returncode == 2
    assert run("errors", "destroy", "-").returncode == 2
    assert run().returncode == 2


def test_render_lists_addresses_and_actions_only():
    d = doc("good")
    text = guard.render(plan(d))
    assert "`create` `google_monitoring_alert_policy.job_failed`" in text
    assert "google_compute_network.ingest" not in text  # no-op
    assert "data.google_project.this" not in text  # read
    assert "create: 2, update: 6" in text
    for secret in ("000000-AAAAAA-111111", "owner@example.com", "203.0.113.10", "sha256", "invented-model"):
        assert secret not in text


def test_render_shows_failed_guards_and_moves():
    text = guard.render(plan(doc("fail_router_moved_into_module")))
    assert "- FAIL `shopify-network-path`" in text
    assert "  - `module.net.google_compute_router.ingest_v2[0]`" in text
    assert "(moved from `google_compute_router.ingest`)" in text


def test_no_changes():
    d = doc("good")
    for r in d["resource_changes"]:
        r["change"]["actions"] = ["no-op"]
    assert "No changes." in guard.render(plan(d))


LEAKY_KEYS = [
    "owner@example.com",  # a variable's value, and an @
    "OWNER.EXAMPLE.COM",  # the same, case and separators changed
    "000000AAAAAA111111",  # the billing id without its dashes
    "203.0.113.20",  # an egress IP output
    "x`y",  # a character the comment won't carry
    "line\nbreak",
    "a b",
]


@pytest.mark.parametrize("key", LEAKY_KEYS)
@pytest.mark.parametrize("where", ["change", "no-op job named by a guard"])
def test_every_printed_address_goes_through_the_gate(key, where):
    d = doc("fail_image_tag" if where != "change" else "good")
    if where == "change":
        extra = copy.deepcopy(find(d, "google_monitoring_alert_policy.job_failed"))
        extra["address"] = f'google_monitoring_alert_policy.by_key["{key}"]'
        d["resource_changes"].append(extra)
    else:  # a no-op job with a tag image: named by ingest-image-pinned, never by the change list
        job = copy.deepcopy(find(d, 'google_cloud_run_v2_job.ingest["merge"]'))
        job["address"], job["change"]["actions"] = f'google_cloud_run_v2_job.ingest["{key}"]', ["no-op"]
        d["resource_changes"].append(job)
    # check prints only the addresses a guard names; render prints every changed one
    for command in ("render",) if where == "change" else ("render", "check"):
        r = run(command, "-", stdin=json.dumps(d))
        assert r.returncode == 2, (command, r.stdout)
        assert r.stdout == ""
        assert key not in r.stderr and key.casefold() not in r.stderr.casefold()


@pytest.mark.parametrize(
    "address",
    [
        "aws_x.y",
        "google_x.y[0]",
        'google_x.y["crawl-shopify"]',
        'module.a["k"].module.b[2].google_x.y',
        "data.google_x.y",
    ],
)
def test_terraform_addresses_pass(address):
    assert guard.Scrubber([]).printable(address) == address


# The owner's cutover script's jq checks, copied verbatim: on its own cases the port names the same addresses.
JQ_CHECKS = {
    "no-delete-or-replace": r""".resource_changes[]? | select(.change.actions|index("delete")) | .address""",
    "shopify-network-path": r"""(.resource_changes[]? | select(.change.actions!=["no-op"] and .change.actions!=["read"])
  | select(.address|test("shopify_egress|^google_compute_(network|router)\\.ingest$")) | select(.type!="google_compute_router_nat") | .address),
  (.resource_changes[]? | select(.address=="google_compute_router_nat.shopify_egress")
  | select([.change.before, .change.after] | map({router, nat_ips, nat_ip_allocate_option, source_subnetwork_ip_ranges_to_nat, subnetwork}) | .[0] != .[1]) | .address)""",
    "shopify-jobs-network": r""".resource_changes[]? | select(.type=="google_cloud_run_v2_job")
  | select(.change.before.name=="crawl-shopify" or .change.before.name=="crawl-retry")
  | select(.change.before.template[0].template[0].vpc_access != .change.after.template[0].template[0].vpc_access) | .address""",
}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize("name", ORIGINAL)
@pytest.mark.parametrize("check", sorted(JQ_CHECKS))
def test_port_matches_the_cutover_scripts_jq(name, check):
    out = subprocess.run(
        ["jq", "-r", JQ_CHECKS[check], str(FIXTURES / f"{name}.json")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    assert results(doc(name))[check] == [line for line in out if line]


@pytest.mark.parametrize("name", sorted(set(FAILING) - set(ORIGINAL)))
def test_new_cases_pass_the_original_jq(name):
    """Documents why the guards were widened: the cutover script's checks let these through."""
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    want = FAILING[name][0]
    if want not in JQ_CHECKS:
        return
    out = subprocess.run(
        ["jq", "-r", JQ_CHECKS[want], str(FIXTURES / f"{name}.json")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == ""


# --- terraform-plan.yml's step scripts, with a stand-in binary -----------------------------------------------------

FAKE = {
    "TF_VAR_billing_account": "012345-ABCDEF-6789AB",
    "TF_VAR_owner_email": "Owner.Person@example.org",
    "TF_VAR_ingest_image": "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/ingest@sha256:"
    + "d" * 64,
    "TF_VAR_ingest_schedule_enabled": "true",
}
LEAK_FORMS = [
    FAKE["TF_VAR_billing_account"],
    FAKE["TF_VAR_billing_account"].lower(),
    FAKE["TF_VAR_billing_account"].replace("-", ""),
    FAKE["TF_VAR_owner_email"],
    FAKE["TF_VAR_owner_email"].lower(),
    "203.0.113.77",
]

# Prints fake secrets the way provider errors do, on stdout and stderr, then fails at $FAKE_FAIL.
STAND_IN = r"""#!/bin/sh
cmd=$1
[ -z "$TF_LOG" ] || { echo "TF_LOG is set" >&2; exit 3; }
[ "$cmd" = show ] || echo "stdout mentions $TF_VAR_owner_email and 203.0.113.77"
if [ "$cmd" = "$FAKE_FAIL" ]; then
  cat >&2 <<EOF
│ Error: Error when reading or editing Budget "billingAccounts/$TF_VAR_billing_account/budgets/x": googleapi: 403
│
│   with google_billing_budget.monthly,
│   on alerts.tf line 14, in resource "google_billing_budget" "monthly":
│   14: resource "google_billing_budget" "monthly" {
│
│ Error: bad value for $(echo "$TF_VAR_owner_email" | tr A-Z a-z) at 203.0.113.77
│
│   with google_monitoring_alert_policy.by_key["$(echo "$TF_VAR_billing_account" | tr -d -)"],
│   on alerts.tf line 40, in resource "google_monitoring_alert_policy" "by_key":
EOF
  exit 1
fi
case "$cmd" in
  plan) for a in "$@"; do case "$a" in -out=*) : > "${a#-out=}" ;; esac; done ;;
  show) cat "$FAKE_PLAN_JSON" ;;
esac
exit 0
"""

GH_STAND_IN = r"""#!/bin/sh
for a in "$@"; do case "$a" in body=@*) cat "${a#body=@}" > "$GH_CAPTURE" ;; esac; done
exit 0
"""


def _steps() -> dict:
    wf = yaml.safe_load(WORKFLOW.read_text())
    return {s.get("id") or s.get("name"): s for job in wf["jobs"].values() for s in job["steps"]}


@pytest.fixture
def runner(tmp_path):
    """A fake runner: a private PATH whose only Terraform is the stand-in, the GitHub files, and a temp folder."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, text in (("terraform", STAND_IN), ("gh", GH_STAND_IN)):
        (bindir / name).write_text(text)
        (bindir / name).chmod(0o755)
    for name in ("python3", "base64", "cat", "rm", "mkdir", "tr", "head", "sh", "bash"):
        target = shutil.which(name)
        if target:
            (bindir / name).symlink_to(sys.executable if name == "python3" else target)
    temp = tmp_path / "runner"
    temp.mkdir()
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(temp),
        "GITHUB_WORKSPACE": str(ROOT),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_REPOSITORY": "owner/repo",
        "GH_CAPTURE": str(tmp_path / "comment.md"),
        "BUCKET": "a-bucket",
        "FAKE_PLAN_JSON": str(FIXTURES / "good.json"),
        "TF_LOG": "TRACE",
        **FAKE,
    }
    for f in ("output", "summary"):
        (tmp_path / f).write_text("")
    return tmp_path, env


def run_step(step_id: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    script = _steps()[step_id]["run"]
    return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=env, capture_output=True, text=True)


def outputs(tmp_path: Path) -> dict:
    out = {}
    for line in (tmp_path / "output").read_text().splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def assert_no_leak(*texts: str):
    for text in texts:
        for form in LEAK_FORMS:
            assert form not in text, f"leaked {form!r}"


@pytest.mark.parametrize("fail_at", ["fmt", "init", "validate", "plan", "show"])
def test_a_failed_terraform_step_shows_no_values(runner, fail_at):
    tmp_path, env = runner
    r = run_step("terraform", {**env, "FAKE_FAIL": fail_at}, ROOT / "src" / "terraform")
    assert r.returncode == 1
    log = r.stdout + r.stderr
    assert_no_leak(log, (tmp_path / "output").read_text(), (tmp_path / "summary").read_text())
    assert f"terraform {fail_at} failed (2 errors)" in log
    assert "at alerts.tf:14" in log and "at alerts.tf:40" in log
    assert "resource google_billing_budget.monthly" in log
    assert "resource (not shown" in log  # the for_each key holding the billing id
    assert outputs(tmp_path)["failed"] == fail_at
    assert not (Path(env["RUNNER_TEMP"]) / "tf-logs").exists()
    assert not (Path(env["RUNNER_TEMP"]) / "tfplan").exists()

    # the comment for that run says the plan errored, at which command, and for which commit
    cenv = {
        **env,
        "RESULT": "failure",
        "FAILED": fail_at,
        "GUARDS": "",
        "COMMENT_B64": "",
        "PR": "7",
        "HEAD_SHA": "f" * 40,
        "MERGE_SHA": "e" * 40,
        "RUN_URL": "https://example.invalid/run",
    }
    c = run_step("Post or update the plan comment", cenv, tmp_path)
    assert c.returncode == 0, c.stderr
    body = (tmp_path / "comment.md").read_text()
    assert f"**The plan errored** at `terraform {fail_at}`" in body
    assert "f" * 40 in body
    assert_no_leak(body)


def test_missing_settings_are_named_not_shown(runner):
    tmp_path, env = runner
    env = {k: v for k, v in env.items() if k != "TF_VAR_ingest_image"}
    r = run_step("terraform", {**env, "FAKE_FAIL": "none"}, ROOT / "src" / "terraform")
    assert r.returncode == 1
    assert "TF_VAR_INGEST_IMAGE" in r.stdout
    assert_no_leak(r.stdout + r.stderr)


def test_a_good_plan_end_to_end(runner):
    tmp_path, env = runner
    r = run_step("terraform", {**env, "FAKE_FAIL": "none"}, ROOT / "src" / "terraform")
    assert r.returncode == 0, r.stdout + r.stderr
    g = run_step("guards", env, ROOT)
    assert g.returncode == 0, g.stdout + g.stderr
    out = outputs(tmp_path)
    assert out["result"] == "pass"
    rendered = base64.b64decode(out["comment"]).decode()
    summary = (tmp_path / "summary").read_text()
    private = [
        "000000-AAAAAA-111111",
        "owner@example.com",
        "203.0.113.10",
        "203.0.113.20",
        "invented-model-1",
    ]
    for text in (r.stdout + r.stderr, g.stdout + g.stderr, rendered, summary):
        assert_no_leak(text)
        assert not any(p in text for p in private)
    assert "`create` `google_monitoring_alert_policy.job_failed`" in rendered
    cenv = {
        **env,
        "RESULT": "success",
        "FAILED": "",
        "GUARDS": "pass",
        "COMMENT_B64": out["comment"],
        "PR": "7",
        "HEAD_SHA": "f" * 40,
        "MERGE_SHA": "e" * 40,
        "RUN_URL": "https://example.invalid/run",
    }
    assert run_step("Post or update the plan comment", cenv, tmp_path).returncode == 0
    body = (tmp_path / "comment.md").read_text()
    assert "Guards: **pass**." in body and "google_monitoring_alert_policy.job_failed" in body


def test_a_guard_failure_is_reported_and_fails_the_step(runner):
    tmp_path, env = runner
    env["FAKE_PLAN_JSON"] = str(FIXTURES / "fail_wif_grant.json")
    assert run_step("terraform", {**env, "FAKE_FAIL": "none"}, ROOT / "src" / "terraform").returncode == 0
    g = run_step("guards", env, ROOT)
    assert g.returncode == 1
    assert "FAIL workload-identity: google_service_account_iam_member.extra" in g.stdout
    assert outputs(tmp_path)["result"] == "fail"


def test_an_unscrubbable_plan_gives_no_change_list(runner):
    tmp_path, env = runner
    d = doc("good")
    find(d, "google_monitoring_alert_policy.job_failed")["address"] = 'google_x.y["owner@example.com"]'
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(d))
    env["FAKE_PLAN_JSON"] = str(bad)
    assert run_step("terraform", {**env, "FAKE_FAIL": "none"}, ROOT / "src" / "terraform").returncode == 0
    g = run_step("guards", env, ROOT)
    assert g.returncode == 1
    out = outputs(tmp_path)
    assert out["result"] == "error" and "comment" not in out
    assert "owner@example.com" not in g.stdout + g.stderr + (tmp_path / "summary").read_text()


@pytest.mark.parametrize(
    ("result", "guards", "expect"),
    [("skipped", "", "**No plan**"), ("cancelled", "", "**Cancelled**"), ("failure", "error", "**Errored**")],
)
def test_the_comment_is_replaced_whatever_happened(runner, result, guards, expect):
    tmp_path, env = runner
    cenv = {
        **env,
        "RESULT": result,
        "FAILED": "",
        "GUARDS": guards,
        "COMMENT_B64": "",
        "PR": "7",
        "HEAD_SHA": "f" * 40,
        "MERGE_SHA": "e" * 40,
        "RUN_URL": "https://example.invalid/run",
    }
    assert run_step("Post or update the plan comment", cenv, tmp_path).returncode == 0
    body = (tmp_path / "comment.md").read_text()
    assert body.startswith("<!-- terraform-plan -->") and expect in body and "f" * 40 in body


def test_workflow_shape():
    wf = yaml.safe_load(WORKFLOW.read_text())
    triggers = wf[True] if True in wf else wf["on"]  # YAML 1.1 reads `on` as true
    assert set(triggers) == {"pull_request"}
    assert wf["permissions"] == {}
    assert wf["jobs"]["plan"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert wf["jobs"]["comment"]["permissions"] == {"pull-requests": "write"}
    for job in wf["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")
    assert "TF_LOG" not in json.dumps(wf["jobs"]["plan"].get("env", {}))
    assert os.path.exists(ROOT / "src" / "terraform" / "ci.tfvars")
    assert not list((ROOT / "src" / "terraform").glob("*.auto.tfvars"))
