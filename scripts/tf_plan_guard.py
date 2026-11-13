#!/usr/bin/env python3
"""Guards and a scrubbed change list for a Terraform plan, read from `terraform show -json <planfile>`.

The guards start from the owner's cutover script's jq checks and widen them from exact addresses to resource types
and what is attached to Shopify's path, so a moved, renamed, indexed or module-wrapped resource can't slip past.
CI (terraform-plan.yml, and the apply workflow) and the owner's shell run the same file.

    python3 -I scripts/tf_plan_guard.py check  PLAN.json   # exit 0 pass, 1 a guard failed, 2 unreadable input
    python3 -I scripts/tf_plan_guard.py render PLAN.json   # Markdown: guard verdicts, changes, totals; exit 2 if
                                                            # it can't be scrubbed (nothing is printed then)
    python3 -I scripts/tf_plan_guard.py errors STEP LOG    # a failed Terraform step's log, reduced to a fixed
                                                            # message, file:line and checked addresses; exit 0

`-` reads the JSON from stdin. Standard library only, so no install step is needed.

The plan JSON holds every variable in clear, sensitive or not, and Terraform's own messages can quote values. This
repository is public, so nothing here prints a value: output is fixed text, guard names, file:line, resource
addresses that pass Terraform's address syntax with plain keys, and the attribute paths that change (lower-case
names; a map key that looks like one, such as a label's, can appear). None may contain a variable value, egress IP
or TF_VAR_* environment value (compared case-folded, with separators removed). Anything else is refused.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

PASS, FAIL, UNREADABLE = 0, 1, 2

ACTIONS = {"no-op", "create", "read", "update", "delete", "forget"}
# Shopify's subnet, IP and NAT: their addresses (cutover script, "SHOP") and their names in GCP.
SHOPIFY_ADDRESS = re.compile(r"shopify_egress")
SHOPIFY_NAME = "shopify-egress"
NETWORK_TYPES = {"google_compute_network", "google_compute_router"}
# An allow-list: the only types that may change while attached to the ingest network or router. Anything else there
# (a route, firewall rule or policy, peering, router peer or interface, VPN tunnel, connector...) could take Shopify's
# egress off its NAT, so it is refused, as is a compute resource whose network or router is unknown at plan.
ON_INGEST_ALLOWED = {"google_compute_subnetwork", "google_compute_router_nat", "google_cloud_run_v2_job"}
NAT_TYPE = "google_compute_router_nat"
# The NAT's attributes that decide which IPs and subnets it serves; everything else (ports, logging) may change.
NAT_PINNED = (
    "router",
    "nat_ips",
    "nat_ip_allocate_option",
    "source_subnetwork_ip_ranges_to_nat",
    "subnetwork",
)
JOB_TYPE = "google_cloud_run_v2_job"
SHOPIFY_JOBS = ("crawl-shopify", "crawl-retry")
WIF_TYPE_PREFIX = "google_iam_workload_identity_pool"  # pools, providers, and IAM on them
WIF_MARKERS = ("workloadidentitypools", "principalset:", "principal:")
WIF_ROLES = {"roles/iam.workloadIdentityUser", "roles/iam.workloadIdentityPoolAdmin"}
DIGEST = "@sha256:[0-9a-f]{64}"
# The accounts whose trust the pipeline must never change: the deploy account (deploy.tf) and the plan and apply
# accounts (src/terraform/bootstrap). Their resources, IAM on them, and any grant to them are the owner's, by hand.
GUARDED_ACCOUNTS = ("registry-coffee-deploy", "registry-coffee-tf-plan", "registry-coffee-tf-apply")
SA_TYPE = "google_service_account"
SA_KEY_TYPE = "google_service_account_key"  # sign-in is Workload Identity Federation: no key, for any account
DEPLOY_ACCOUNT = "registry-coffee-deploy"
# The deploy account's own grants (deploy.tf), exactly: its registry writer and service developer roles.
DEPLOY_GRANTS = {
    ("google_artifact_registry_repository_iam_member", "roles/artifactregistry.writer"),
    ("google_cloud_run_v2_service_iam_member", "roles/run.developer"),
}
CUSTOM_ROLE_TYPES = ("google_project_iam_custom_role", "google_organization_iam_custom_role")
# Roles that let their holder act as, or make keys for, service accounts: refused anywhere they'd be granted.
IMPERSONATION_ROLES = {
    "roles/iam.serviceAccountTokenCreator",
    "roles/iam.serviceAccountUser",
    "roles/iam.serviceAccountKeyAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountOpenIdTokenCreator",
    "roles/iam.roleAdmin",
    "roles/iam.securityAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/owner",
    "roles/editor",
}
# (roles/iam.workloadIdentityUser is the workload-identity guard's.)
NUMERIC_ACCOUNT = re.compile(r"serviceaccounts/[0-9]+$")

# Terraform's address syntax, with for_each keys limited to plain characters (no quotes, spaces, @, /, backticks).
_KEY = r'(?:\[(?:[0-9]+|"[A-Za-z0-9_.-]+")\])?'
ADDRESS = re.compile(
    rf"(?:module\.[A-Za-z_][A-Za-z0-9_-]*{_KEY}\.)*(?:data\.)?[a-z0-9_]+\.[A-Za-z_][A-Za-z0-9_-]*{_KEY}"
)
ATTRIBUTE = re.compile(r"[a-z][a-z0-9_]{0,40}")
FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.tf(?:vars)?")
STEPS = ("fmt", "init", "validate", "plan", "show", "apply")
MIN_SCRUB_LEN = 6  # shorter strings would match addresses by accident; compared after normalize()


class Unreadable(Exception):
    """The input isn't a plan we can judge, or can't be printed safely: every caller fails closed on it.

    Every message is fixed text: it never quotes the plan."""


def jq(value, *path):
    """jq's `.a.b[0]` semantics: a missing key, an index past the end, or null along the way gives None.

    Indexing the wrong type (a key into a list, an index into a string) is an error in jq, and the cutover script
    stopped on it (set -e); here it raises Unreadable, so the guard fails closed the same way.
    """
    for step in path:
        if value is None:
            return None
        if not isinstance(value, list if isinstance(step, int) else dict):
            raise Unreadable("a resource change has a shape the guard doesn't understand")
        if isinstance(step, int):
            value = value[step] if -len(value) <= step < len(value) else None
        else:
            value = value.get(step)
    return value


def jq_equal(a, b) -> bool:
    """jq's `==`: numbers by value, booleans never equal to numbers, objects key by key."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(jq_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(jq_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


class Scrubber:
    """The one gate every printed address goes through."""

    def __init__(self, private: Iterable[str]):
        self.private = {n for n in map(normalize, private) if len(n) >= MIN_SCRUB_LEN}

    def printable(self, address: str) -> str:
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise Unreadable("an address is outside Terraform's address syntax with plain keys")
        n = normalize(address)
        if any(p in n for p in self.private):
            raise Unreadable("an address contains a value from the plan")
        return address

    def attribute(self, path: str) -> str | None:
        """An attribute path such as `template.containers.image`, or None if it can't be shown: every segment a
        schema-style name (map keys that are data, such as labels or env names, rarely are), and no private value."""
        if not all(ATTRIBUTE.fullmatch(seg) for seg in path.split(".")):
            return None
        n = normalize(path)
        return None if any(p in n for p in self.private) else path


@dataclass
class Plan:
    changes: list[dict]
    variables: dict
    raw: dict = field(repr=False)

    def var(self, name: str):
        return jq(self.variables, name, "value")

    def scrubber(self) -> Scrubber:
        found = _strings({k: jq(v, "value") for k, v in self.variables.items() if isinstance(v, dict)})
        for source in (jq(self.raw, "planned_values", "outputs"), jq(self.raw, "output_changes")):
            if isinstance(source, dict):
                # the values only: an output's `actions` or `type` ("create", "string") would match addresses
                found += _strings(
                    [
                        [jq(v, f) for f in ("value", "before", "after")]
                        for k, v in source.items()
                        if k.endswith("egress_ip") and isinstance(v, dict)
                    ]
                )
        return Scrubber(found)


def load(text: str) -> Plan:
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise Unreadable("not JSON") from e
    if not isinstance(doc, dict):
        raise Unreadable("not `terraform show -json` output")
    version = doc.get("format_version")
    if not isinstance(version, str) or version.split(".")[0] != "1":
        raise Unreadable("not `terraform show -json` output in format 1.x")
    if doc.get("errored") is True or doc.get("complete") is False:
        raise Unreadable("the plan errored or is incomplete")
    changes, variables = doc.get("resource_changes"), doc.get("variables")
    if not isinstance(changes, list) or not isinstance(variables, dict):
        raise Unreadable("resource_changes or variables is missing or has the wrong shape")
    for rc in changes:
        if not isinstance(rc, dict) or not all(isinstance(rc.get(k), str) for k in ("address", "type")):
            raise Unreadable("a resource change has no address or type")
        if not isinstance(rc.get("previous_address", ""), str) or not isinstance(rc.get("change"), dict):
            raise Unreadable("a resource change has the wrong shape")
        acts = rc["change"].get("actions")
        if (
            not isinstance(acts, list)
            or not acts
            or not all(isinstance(a, str) for a in acts)
            or not set(acts) <= ACTIONS
        ):
            raise Unreadable("a resource change has no actions, or one the guard doesn't know")
    return Plan(changes=changes, variables=variables, raw=doc)


def actions(rc: dict) -> list[str]:
    return rc["change"]["actions"]


def is_change(rc: dict) -> bool:
    return actions(rc) not in (["no-op"], ["read"])


def importing(rc: dict) -> bool:
    return jq(rc, "change", "importing") is not None


def moved(rc: dict) -> bool:
    return bool(rc.get("previous_address")) and rc.get("previous_address") != rc["address"]


def touched(rc: dict) -> bool:
    """A change, an import or a move: anything the approver should see."""
    return is_change(rc) or importing(rc) or moved(rc)


def addresses(rc: dict) -> list[str]:
    return [a for a in (rc["address"], rc.get("previous_address")) if a]


def unknown(rc: dict, *path) -> bool:
    """after_unknown marks a value Terraform learns only at apply: `true` at the path or at any parent of it."""
    value = jq(rc, "change", "after_unknown")
    for step in path:
        if value is True:
            return True
        if not isinstance(value, list if isinstance(step, int) else dict):
            return False
        value = jq(value, step)
    return value is True


# --- The guards: each returns the addresses that break it (empty = pass). -----------------------------------------


def no_delete_or_replace(plan: Plan) -> list[str]:
    """Never destroy, replace or stop managing anything: no action is `delete` or `forget`."""
    return [rc["address"] for rc in plan.changes if {"delete", "forget"} & set(actions(rc))]


def _nat_subnet_names(nat) -> list[str]:
    subnets = jq(nat, "subnetwork")
    if subnets is None:
        return []
    if not isinstance(subnets, list):
        raise Unreadable("a NAT's subnetwork list has a shape the guard doesn't understand")
    # a subnet whose name is unknown (null at plan) might be Shopify's: count it as Shopify's, so the NAT is protected
    return [n if isinstance(n, str) else SHOPIFY_NAME for n in (jq(s, "name") for s in subnets)]


def _shopify_nat(rc: dict) -> bool:
    before, after = jq(rc, "change", "before"), jq(rc, "change", "after")
    names = [jq(before, "name"), jq(after, "name")] + _nat_subnet_names(before) + _nat_subnet_names(after)
    return any(SHOPIFY_ADDRESS.search(a) for a in addresses(rc)) or any(
        isinstance(n, str) and SHOPIFY_NAME in n for n in names
    )


ATTACHMENT_FIELDS = {"network", "router", "peer_network", "attachment_target", "network_url"}


def _attachments(value) -> list[str]:
    """The strings under attachment fields (network, router...), at any depth: what a resource is attached to."""
    if isinstance(value, list):
        return [s for v in value for s in _attachments(v)]
    if isinstance(value, dict):
        return [
            s for k, v in value.items() for s in (_strings(v) if k in ATTACHMENT_FIELDS else _attachments(v))
        ]
    return []


def shopify_network_path(plan: Plan) -> list[str]:
    """Shopify's path stays as it is: no change to any network or router; on the ingest network or router, changes
    only to the allowed types (ON_INGEST_ALLOWED); no new NAT on the ingest router; a NAT
    that serves Shopify's subnet (by address, name or subnet, before or after) changes only its port and logging
    settings; and nothing else named for Shopify's egress (its subnet, its IP) changes."""
    service = plan.var("service")
    if not isinstance(service, str) or not service:
        raise Unreadable("var.service is missing from the plan")
    router = f"{service}-ingest"
    bad = []
    for rc in plan.changes:
        if not is_change(rc):
            continue
        before, after = jq(rc, "change", "before"), jq(rc, "change", "after")
        if rc["type"] in NETWORK_TYPES:
            bad.append(rc["address"])
        elif rc["type"] not in ON_INGEST_ALLOWED and (
            any(
                t == router or t.endswith((f"/networks/{router}", f"/routers/{router}"))
                for t in _attachments([before, after])
            )
            or (
                rc["type"].startswith("google_compute_")
                and any(unknown(rc, k) for k in ("network", "router"))
            )
        ):
            bad.append(
                rc["address"]
            )  # attached to the ingest network or router (by name, id or link), or unknown
        elif rc["type"] == NAT_TYPE:
            # a new NAT on the ingest router, or on a router not known until apply
            if "create" in actions(rc) and (jq(after, "router") in (router, None) or unknown(rc, "router")):
                bad.append(rc["address"])
            elif (unknown(rc, "subnetwork") or _shopify_nat(rc)) and not all(
                jq_equal(jq(before, k), jq(after, k)) for k in NAT_PINNED
            ):
                bad.append(rc["address"])  # a NAT serving Shopify's subnet changes more than its ports
        elif any(SHOPIFY_ADDRESS.search(a) for a in addresses(rc)) or any(
            isinstance(n, str) and SHOPIFY_NAME in n for n in (jq(before, "name"), jq(after, "name"))
        ):
            bad.append(rc["address"])
    return bad


def shopify_jobs_network(plan: Plan) -> list[str]:
    """crawl-shopify and crawl-retry keep their network (vpc_access) as it is."""
    bad = []
    path = ("template", 0, "template", 0, "vpc_access")
    for rc in plan.changes:
        if rc["type"] != JOB_TYPE or jq(rc, "change", "before", "name") not in SHOPIFY_JOBS:
            continue
        if not jq_equal(jq(rc, "change", "before", *path), jq(rc, "change", "after", *path)):
            bad.append(rc["address"])
    return bad


def ingest_image_pinned(plan: Plan) -> list[str]:
    """The ingest image is the build's own repository, pinned by digest, and every job runs exactly that image (an
    image from another registry, or one only known at apply, fails)."""
    bad = []
    region, project, service = (plan.var(n) for n in ("region", "project_id", "service"))
    image = plan.var("ingest_image")
    if not all(isinstance(v, str) and v for v in (region, project, service)):
        bad.append("var.region")
    else:
        repo = re.escape(f"{region}-docker.pkg.dev/{project}/{service}/ingest")
        if not (isinstance(image, str) and re.fullmatch(repo + DIGEST, image)):
            bad.append("var.ingest_image")
    pinned = bad == []  # with no valid pin, every job's image fails too
    containers = ("template", 0, "template", 0, "containers")
    for rc in plan.changes:
        if rc["type"] != JOB_TYPE or actions(rc) == ["delete"]:
            continue
        listed = jq(rc, "change", "after", *containers)
        images = [jq(c, "image") for c in listed] if isinstance(listed, list) and listed else [None]
        # exactly var.ingest_image: a digest from another registry, or an image unknown at plan, fails
        if not (pinned and all(i == image for i in images)):
            bad.append(rc["address"])
    return bad


SERVICE_TYPE = "google_cloud_run_v2_service"
# cloud_run.tf creates the service with Google's sample image; deploys put the app's in.
SERVICE_PLACEHOLDER = "us-docker.pkg.dev/cloudrun/container/hello"


def app_image_pinned(plan: Plan) -> list[str]:
    """A Cloud Run service whose image Terraform would set (created, changed, or unknown at plan) gets the app's own
    repository, pinned by digest: the ingest image's rule. Deploys set the image outside Terraform (ignore_changes),
    so an unchanged image passes."""
    region, project, service = (plan.var(n) for n in ("region", "project_id", "service"))
    pinned = re.compile(re.escape(f"{region}-docker.pkg.dev/{project}/{service}/app") + DIGEST)
    containers = ("template", 0, "containers")
    bad = []
    for rc in plan.changes:
        if rc["type"] != SERVICE_TYPE or not is_change(rc) or actions(rc) == ["delete"]:
            continue
        before, after = jq(rc, "change", "before", *containers), jq(rc, "change", "after", *containers)
        images = [jq(c, "image") for c in after] if isinstance(after, list) and after else [None]
        old = [jq(c, "image") for c in before] if isinstance(before, list) else None
        if images == old and not unknown(rc, *containers):
            continue
        if actions(rc) == ["create"] and images == [SERVICE_PLACEHOLDER]:
            continue
        if not all(isinstance(i, str) and pinned.fullmatch(i) for i in images):
            bad.append(rc["address"])
    return bad


def workload_identity(plan: Plan) -> list[str]:
    """No change to a workload identity pool or provider, or to an IAM grant that names one (or grants the
    workload identity roles): the gate isn't managed by what it gates."""
    bad = []
    for rc in plan.changes:
        if not (is_change(rc) or importing(rc)):
            continue
        if rc["type"].startswith(WIF_TYPE_PREFIX):
            bad.append(rc["address"])
        elif "_iam_" in rc["type"]:
            sides = [jq(rc, "change", "before"), jq(rc, "change", "after")]
            text = [s.casefold() for s in _strings(sides)]
            roles = {jq(s, "role") for s in sides if s is not None}
            if any(m in s for s in text for m in WIF_MARKERS) or roles & WIF_ROLES or unknown(rc, "role"):
                bad.append(rc["address"])
    return bad


def _names(text: list[str], *accounts: str) -> bool:
    """An account named by email, bare id or resource path in any of these (case-folded) strings."""
    return any(f"{a}@" in s or s == a or s.endswith(f"/{a}") for s in text for a in accounts)


def guarded_accounts(plan: Plan) -> list[str]:
    """No change to the deploy, plan or apply account itself, and no other resource naming the plan or apply account;
    no service account key for any account; no custom role, created, changed or granted; no grant of a role that
    impersonates, makes keys or administers roles, on any resource, to anyone; no grant naming the plan or apply
    account; none naming the deploy account but its registry writer and service developer roles; no service account
    IAM naming its account by numeric id. In project and service account IAM, a role, account or policy unknown at
    plan fails closed, and so does an unknown member unless the plan creates (non-guarded) accounts it can be."""
    creates_accounts = any(rc["type"] == SA_TYPE and "create" in actions(rc) for rc in plan.changes)
    ci = [a for a in GUARDED_ACCOUNTS if a != DEPLOY_ACCOUNT]
    bad = []
    for rc in plan.changes:
        if not (is_change(rc) or importing(rc)):
            continue
        sides = [jq(rc, "change", "before"), jq(rc, "change", "after")]
        strings = _strings(sides)
        text = [s.casefold() for s in strings]
        rtype = rc["type"]
        if rtype == SA_TYPE:
            refuse = _names(text, *GUARDED_ACCOUNTS) or unknown(rc, "account_id")
        elif rtype == SA_KEY_TYPE or rtype in CUSTOM_ROLE_TYPES:
            refuse = True
        elif "_iam_" in rtype:
            roles = {s for s in strings if s.startswith(("roles/", "projects/", "organizations/"))} | {
                r for s in text for r in IMPERSONATION_ROLES if r.casefold() in s
            }
            roles = {r for r in roles if "/roles/" in r or r.startswith("roles/")}
            sa_or_project = rtype.startswith(("google_service_account_iam_", "google_project_iam_"))
            refuse = (
                _names(text, *ci)
                or (_names(text, DEPLOY_ACCOUNT) and not {(rtype, r) for r in roles} <= DEPLOY_GRANTS)
                or bool(roles & IMPERSONATION_ROLES)
                or any(not r.startswith("roles/") for r in roles)
                or (
                    rtype.startswith("google_service_account_iam_")
                    and any(NUMERIC_ACCOUNT.search(s) for s in text)
                )
                or (
                    sa_or_project
                    and any(unknown(rc, k) for k in ("service_account_id", "policy_data", "role"))
                )
                or (
                    sa_or_project
                    and not creates_accounts
                    and any(unknown(rc, k) for k in ("member", "members"))
                )
            )
        else:
            refuse = _names(text, *ci)  # e.g. a job or schedule that would run as the plan or apply account
        if refuse:
            bad.append(rc["address"])
    return bad


GUARDS: list[tuple[str, str, Callable[[Plan], list[str]]]] = [
    ("no-delete-or-replace", "nothing is destroyed, replaced or forgotten", no_delete_or_replace),
    (
        "shopify-network-path",
        "the network, the router and Shopify's NAT, subnet and IP: unchanged",
        shopify_network_path,
    ),
    ("shopify-jobs-network", "crawl-shopify and crawl-retry keep their network", shopify_jobs_network),
    ("ingest-image-pinned", "the ingest image is pinned by digest", ingest_image_pinned),
    ("app-image-pinned", "a service image Terraform sets is the app's, pinned by digest", app_image_pinned),
    ("workload-identity", "no workload identity pool, provider or grant on them changes", workload_identity),
    (
        "guarded-accounts",
        "the deploy, plan and apply accounts, grants to them, keys and impersonation roles: unchanged",
        guarded_accounts,
    ),
]


def run_guards(plan: Plan) -> list[tuple[str, str, list[str]]]:
    """Every guard, with every address it names already through the scrubber (or Unreadable)."""
    scrub = plan.scrubber()
    return [(name, what, [scrub.printable(a) for a in fn(plan)]) for name, what, fn in GUARDS]


# --- Output ---------------------------------------------------------------------------------------------------------


def changed_paths(before, after, unknown_after, prefix: str = "", depth: int = 5) -> list[str]:
    """Where `before` and `after` differ, as attribute paths (list indexes dropped), at most `depth` deep."""
    if depth == 0 or not (isinstance(after, dict | list) or isinstance(before, dict | list)):
        return [prefix] if prefix else []
    if isinstance(after, list) or isinstance(before, list):
        b, a = before if isinstance(before, list) else [], after if isinstance(after, list) else []
        u = unknown_after if isinstance(unknown_after, list) else []
        if len(a) != len(b) or not all(isinstance(x, dict) for x in a + b):
            return [prefix] if prefix else []
        out = []
        for i in range(len(a)):
            out += changed_paths(b[i], a[i], u[i] if i < len(u) else None, prefix, depth)
        return out
    b, a = before if isinstance(before, dict) else {}, after if isinstance(after, dict) else {}
    u = unknown_after if isinstance(unknown_after, dict) else {}
    out = []
    for key in sorted(set(a) | set(b) | {k for k, v in u.items() if v is True}):
        if jq_equal(b.get(key), a.get(key)) and u.get(key) is not True:
            continue
        path = f"{prefix}.{key}" if prefix else str(key)
        if u.get(key) is True or not isinstance(a.get(key), dict | list):
            out.append(path)
        else:
            out += changed_paths(b.get(key), a.get(key), u.get(key), path, depth - 1) or [path]
    return out


def _what_changes(rc: dict, scrub: Scrubber) -> str:
    if actions(rc) != ["update"]:
        return ""
    paths = changed_paths(
        jq(rc, "change", "before"), jq(rc, "change", "after"), jq(rc, "change", "after_unknown")
    )
    shown = sorted({p for p in (scrub.attribute(x) for x in paths) if p})
    hidden = len(set(paths)) - len(shown)
    if not shown and not hidden:
        return ""
    more = f", and {hidden} more not shown" if hidden else ""
    return f": {', '.join(shown[:8])}" + (f", +{len(shown) - 8}" if len(shown) > 8 else "") + more


def render(plan: Plan) -> str:
    scrub = plan.scrubber()
    changed = sorted((rc for rc in plan.changes if touched(rc)), key=lambda rc: rc["address"])
    lines = ["### Guards", ""]
    for name, what, bad in run_guards(plan):
        lines.append(f"- {'FAIL' if bad else 'pass'} `{name}`: {what}")
        lines += [f"  - `{b}`" for b in bad]
    lines += ["", "### Changes", ""]
    for rc in changed:
        lines.append(
            f"- `{'+'.join(actions(rc))}` `{scrub.printable(rc['address'])}`"
            + (f" (moved from `{scrub.printable(rc['previous_address'])}`)" if moved(rc) else "")
            + (" (import)" if importing(rc) else "")
            + _what_changes(rc, scrub)
        )
    if not changed:
        lines.append("No changes.")
    counts: dict[str, int] = {}
    for rc in changed:
        counts["+".join(actions(rc))] = counts.get("+".join(actions(rc)), 0) + 1
    lines += ["", "### Totals", ""]
    lines.append(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) if counts else "0 changes")
    return "\n".join(lines) + "\n"


def check_text(plan: Plan) -> tuple[str, bool]:
    results = run_guards(plan)
    out = [
        f"{'FAIL' if bad else 'PASS'} {name}" + (": " + ", ".join(bad) if bad else "")
        for name, _, bad in results
    ]
    return "\n".join(out) + "\n", any(bad for _, _, bad in results)


def errors_text(step: str, log: str, env: dict[str, str]) -> str:
    """A failed Terraform step's log, reduced to what can't hold a value: a fixed message with the error count, the
    file:line of each error, and the resource addresses that pass the scrubber (TF_VAR_* values as the secrets)."""
    scrub = Scrubber(v for k, v in env.items() if k.startswith("TF_VAR_"))
    lines = log.splitlines()
    count = sum(1 for line in lines if re.match(r"^\W*Error: ", line))
    out = [
        f"::error::terraform {step} failed ({count} error{'s' if count != 1 else ''}). Terraform's own messages are"
        " not shown here: they can hold values from the plan. Run it in a shell for the details."
    ]
    seen = set()
    for line in lines:
        where = re.match(r"^\W*on (\S+) line ([0-9]+)", line)
        what = re.match(r"^\W*with (\S+),$", line)
        if where and FILE.fullmatch(where[1]):
            item = f"  at {where[1]}:{where[2]}"
        elif what:
            try:
                item = f"  resource {scrub.printable(what[1])}"
            except Unreadable:
                item = "  resource (not shown: it doesn't pass the scrubber)"
        elif step == "fmt" and FILE.fullmatch(line.strip()):
            item = f"  not formatted: {line.strip()}"
        else:
            continue
        if item not in seen:
            seen.add(item)
            out.append(item)
    return "\n".join(out) + "\n"


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "errors" and argv[1] in STEPS:
        try:
            log = _read(argv[2])
        except OSError:
            log = ""
        sys.stdout.write(errors_text(argv[1], log, dict(os.environ)))
        return PASS
    if len(argv) != 2 or argv[0] not in ("check", "render"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return UNREADABLE
    command, path = argv
    try:
        plan = load(_read(path))
        if command == "render":
            sys.stdout.write(render(plan))
            return PASS
        out, failed = check_text(plan)
    except (OSError, Unreadable) as e:
        reason = e if isinstance(e, Unreadable) else "cannot read the plan file"
        print(f"tf_plan_guard: refusing: {reason}", file=sys.stderr)
        return UNREADABLE
    except Exception:  # any other shape the guard didn't foresee: fixed text, never the input, and exit 2
        print("tf_plan_guard: refusing: the plan has a shape the guard doesn't understand", file=sys.stderr)
        return UNREADABLE
    sys.stdout.write(out)
    return FAIL if failed else PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
