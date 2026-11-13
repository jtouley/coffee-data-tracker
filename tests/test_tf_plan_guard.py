"""scripts/tf_plan_guard.py: the plan guards and the scrubbed change list, on hand-made `terraform show -json` plans.

The fixtures (tests/fixtures/tf_plan/) hold invented values only. One good plan, and one per guard that must fail.
"""

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tf_plan_guard.py"
FIXTURES = ROOT / "tests" / "fixtures" / "tf_plan"

_spec = importlib.util.spec_from_file_location("tf_plan_guard", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
sys.modules["tf_plan_guard"] = guard
_spec.loader.exec_module(guard)


def doc(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def plan(d: dict):
    return guard.load(json.dumps(d))


def run(*args, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args], input=stdin, capture_output=True, text=True
    )


# fixture -> the one guard it must fail, and the addresses it names
FAILING = {
    "fail_delete_replace": ("no-delete-or-replace", ["delete+create google_storage_bucket.ingest_staging"]),
    "fail_shopify_subnet": ("shopify-network-path", ["google_compute_subnetwork.shopify_egress"]),
    "fail_router": ("shopify-network-path", ["google_compute_router.ingest"]),
    "fail_nat_ips": ("shopify-network-path", ["google_compute_router_nat.shopify_egress"]),
    "fail_shopify_job_network": ("shopify-jobs-network", ['google_cloud_run_v2_job.ingest["crawl-retry"]']),
    "fail_image_tag": ("ingest-image-pinned", None),
}


def test_good_plan_passes_every_guard():
    assert all(not bad for _, _, bad in guard.run_guards(plan(doc("good"))))
    r = run("check", str(FIXTURES / "good.json"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("PASS") == len(guard.GUARDS)


@pytest.mark.parametrize("name", sorted(FAILING))
def test_each_failing_plan_fails_its_guard_only(name):
    want, addresses = FAILING[name]
    results = {n: bad for n, _, bad in guard.run_guards(plan(doc(name)))}
    assert results.pop(want), f"{name} should fail {want}"
    assert not any(results.values()), f"{name} fails other guards too: {results}"
    if addresses is not None:
        assert guard.run_guards(plan(doc(name)))[[n for n, _, _ in guard.GUARDS].index(want)][2] == addresses
    r = run("check", str(FIXTURES / f"{name}.json"))
    assert r.returncode == 1
    assert f"FAIL {want}" in r.stdout


def test_every_fixture_is_listed():
    assert {p.stem for p in FIXTURES.glob("*.json")} == {"good", *FAILING}


def test_nat_port_settings_may_change_but_not_its_subnetwork():
    d = doc("good")
    nat = next(r for r in d["resource_changes"] if r["address"] == "google_compute_router_nat.shopify_egress")
    nat["change"]["after"]["max_ports_per_vm"] = 16384
    nat["change"]["after"]["log_config"] = [{"enable": True, "filter": "ALL"}]
    assert guard.shopify_network_path(plan(d)) == []
    nat["change"]["after"]["subnetwork"][0]["source_ip_ranges_to_nat"] = ["PRIMARY_IP_RANGE"]
    assert guard.shopify_network_path(plan(d)) == ["google_compute_router_nat.shopify_egress"]


def test_creating_the_shopify_nat_counts_as_a_change():
    # jq's `null | {router}` is {"router": null}: a create differs from its (null) before.
    d = doc("good")
    nat = next(r for r in d["resource_changes"] if r["address"] == "google_compute_router_nat.shopify_egress")
    nat["change"]["actions"], nat["change"]["before"] = ["create"], None
    assert guard.shopify_network_path(plan(d)) == ["google_compute_router_nat.shopify_egress"]


def test_the_second_egress_may_change():
    d = doc("good")
    d["resource_changes"].append(
        {
            "address": "google_compute_router_nat.woo_generic_egress",
            "type": "google_compute_router_nat",
            "change": {"actions": ["create"], "before": None, "after": {"router": "registry-coffee-ingest"}},
        }
    )
    assert guard.shopify_network_path(plan(d)) == []


def test_shopify_job_network_compared_like_jq():
    d = doc("good")
    job = next(r for r in d["resource_changes"] if r["address"].endswith('["crawl-shopify"]'))
    job["change"]["after"]["template"][0]["template"][0]["vpc_access"] = []
    assert guard.shopify_jobs_network(plan(d)) == ['google_cloud_run_v2_job.ingest["crawl-shopify"]']
    # a new job (before is null) isn't one of Shopify's existing jobs: the jq check skips it too
    job["change"]["before"] = None
    assert guard.shopify_jobs_network(plan(d)) == []


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


def test_image_unknown_until_apply_is_left_to_the_variable_check():
    d = doc("good")
    job = next(r for r in d["resource_changes"] if r["address"].endswith('["merge"]'))
    del job["change"]["after"]["template"][0]["template"][0]["containers"][0]["image"]
    assert guard.ingest_image_pinned(plan(d)) == ['google_cloud_run_v2_job.ingest["merge"]']
    job["change"]["after_unknown"] = {"template": [{"template": [{"containers": True}]}]}
    assert guard.ingest_image_pinned(plan(d)) == []


def test_missing_variables_fail_closed():
    d = doc("good")
    del d["variables"]["project_id"]
    assert guard.ingest_image_pinned(plan(d))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        json.dumps({"resource_changes": []}),  # no format_version
        json.dumps({"format_version": "1.2", "errored": True}),
        json.dumps({"format_version": "1.2", "resource_changes": [{"address": "a.b", "change": {}}]}),
        json.dumps({"format_version": "1.2", "resource_changes": {"a": 1}}),
    ],
)
def test_unreadable_input_fails_closed(text):
    for command in ("check", "render"):
        r = run(command, "-", stdin=text)
        assert r.returncode == 2, (command, text, r.stdout)
        assert r.stdout == ""


def test_wrong_shape_inside_a_change_fails_closed():
    d = doc("good")
    job = next(r for r in d["resource_changes"] if r["address"].endswith('["crawl-shopify"]'))
    job["change"]["before"]["template"] = {"not": "a list"}
    r = run("check", "-", stdin=json.dumps(d))
    assert r.returncode == 2


def test_missing_file_and_bad_usage():
    assert run("check", str(FIXTURES / "nope.json")).returncode == 2
    assert run("plan", str(FIXTURES / "good.json")).returncode == 2
    assert run().returncode == 2


def test_render_lists_addresses_and_actions_only():
    d = doc("good")
    text = guard.render(plan(d))
    assert "`create` `google_monitoring_alert_policy.job_failed`" in text
    assert "google_compute_network.ingest" not in text  # no-op
    assert "data.google_project.this" not in text  # read
    assert "create: 1, update: 5" in text
    for v in guard.private_strings(plan(d)):
        assert v not in text
    for secret in ("000000-AAAAAA-111111", "owner@example.com", "203.0.113.10", "sha256"):
        assert secret not in text


def test_render_shows_failed_guards():
    text = guard.render(plan(doc("fail_router")))
    assert "- FAIL `shopify-network-path`" in text
    assert "  - `google_compute_router.ingest`" in text


def test_no_changes():
    d = doc("good")
    for r in d["resource_changes"]:
        r["change"]["actions"] = ["no-op"]
    assert "No changes." in guard.render(plan(d))


@pytest.mark.parametrize(
    "key",
    [
        "owner@example.com",  # a variable's value
        "203.0.113.20",  # an egress IP output
        "x`y",  # a character the comment won't carry
        "line\nbreak",
    ],
)
def test_render_refuses_an_address_that_would_leak(key):
    d = doc("good")
    extra = copy.deepcopy(d["resource_changes"][-2])
    extra["address"] = f'google_monitoring_alert_policy.by_key["{key}"]'
    d["resource_changes"].append(extra)
    r = run("render", "-", stdin=json.dumps(d))
    assert r.returncode == 2
    assert r.stdout == ""
    assert key not in r.stderr


def test_check_output_is_scrubbed_too():
    d = doc("fail_delete_replace")
    d["resource_changes"][-1]["address"] = 'google_storage_bucket.b["owner@example.com"]'
    r = run("check", "-", stdin=json.dumps(d))
    assert r.returncode == 2
    assert "owner@example.com" not in r.stdout + r.stderr


# The owner's cutover script's jq checks, copied verbatim: the port must name the same addresses on every fixture.
JQ_CHECKS = {
    "no-delete-or-replace": r'''.resource_changes[]? | select(.change.actions|index("delete")) | "\(.change.actions|join("+")) \(.address)"''',
    "shopify-network-path": r"""(.resource_changes[]? | select(.change.actions!=["no-op"] and .change.actions!=["read"])
  | select(.address|test("shopify_egress|^google_compute_(network|router)\\.ingest$")) | select(.type!="google_compute_router_nat") | .address),
  (.resource_changes[]? | select(.address=="google_compute_router_nat.shopify_egress")
  | select([.change.before, .change.after] | map({router, nat_ips, nat_ip_allocate_option, source_subnetwork_ip_ranges_to_nat, subnetwork}) | .[0] != .[1]) | .address)""",
    "shopify-jobs-network": r""".resource_changes[]? | select(.type=="google_cloud_run_v2_job")
  | select(.change.before.name=="crawl-shopify" or .change.before.name=="crawl-retry")
  | select(.change.before.template[0].template[0].vpc_access != .change.after.template[0].template[0].vpc_access) | .address""",
}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize("name", ["good", *sorted(FAILING)])
@pytest.mark.parametrize("check", sorted(JQ_CHECKS))
def test_port_matches_the_cutover_scripts_jq(name, check):
    out = subprocess.run(
        ["jq", "-r", JQ_CHECKS[check], str(FIXTURES / f"{name}.json")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    ported = dict((n, bad) for n, _, bad in guard.run_guards(plan(doc(name))))[check]
    assert ported == [line for line in out if line]
