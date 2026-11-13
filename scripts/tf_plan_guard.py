#!/usr/bin/env python3
"""Guards and a scrubbed change list for a Terraform plan, read from `terraform show -json <planfile>`.

The guards are the owner's cutover script's jq checks, ported one for one, plus a check that the ingest image is
pinned by digest. CI (terraform-plan.yml, and the apply workflow) and the owner's shell run the same file.

    python3 -I scripts/tf_plan_guard.py check  PLAN.json   # exit 0 pass, 1 a guard failed, 2 unreadable input
    python3 -I scripts/tf_plan_guard.py render PLAN.json   # Markdown: guard verdicts, changes, totals; exit 2 if
                                                            # it can't be scrubbed (nothing is printed then)

`-` reads the JSON from stdin. Standard library only, so no install step is needed.

The plan JSON holds every variable in clear, sensitive or not, and this repository is public: nothing here prints
a value from the plan. Output is resource addresses, actions and guard names only, and `render` refuses (exit 2,
no output) if an address contains anything outside a conservative character set or any string the plan holds as
a variable value or an egress IP output.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

PASS, FAIL, UNREADABLE = 0, 1, 2

# The protected network path: Shopify's subnet, IP and NAT, the network and the router (cutover script, "SHOP").
SHOPIFY_PATH = re.compile(r"shopify_egress|^google_compute_(network|router)\.ingest$")
SHOPIFY_NAT = "google_compute_router_nat.shopify_egress"
# The NAT's attributes that decide which IPs and subnets it serves; everything else (ports, logging) may change.
NAT_PINNED = (
    "router",
    "nat_ips",
    "nat_ip_allocate_option",
    "source_subnetwork_ip_ranges_to_nat",
    "subnetwork",
)
SHOPIFY_JOBS = ("crawl-shopify", "crawl-retry")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")  # .pattern is reused in a fullmatch, so no anchor at the start
# Characters an address may hold in the rendered list. Anything else (a backtick, a newline, a for_each key with
# odd text) refuses the render rather than risk posting it.
SAFE_ADDRESS = re.compile(r'^[A-Za-z0-9_.\-\[\]"/:@ ]+$')
MIN_SCRUB_LEN = 6  # shorter strings ("true", "1Gi", a region code) would match addresses by accident


class Unreadable(Exception):
    """The input isn't a plan we can judge: every caller fails closed on it."""


_MISSING = object()


def jq(value, *path):
    """jq's `.a.b[0]` semantics: a missing key, an index past the end, or null along the way gives None.

    Indexing the wrong type (a key into a list, an index into a string) is an error in jq, and the cutover script
    stopped on it (set -e); here it raises Unreadable, so the guard fails closed the same way.
    """
    for step in path:
        if value is None:
            return None
        if isinstance(step, int):
            if not isinstance(value, list):
                raise Unreadable(f"expected a list at {path!r}")
            value = value[step] if -len(value) <= step < len(value) else None
        else:
            if not isinstance(value, dict):
                raise Unreadable(f"expected an object at {path!r}")
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


@dataclass
class Plan:
    changes: list[dict]
    variables: dict
    raw: dict = field(repr=False)


def load(text: str) -> Plan:
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise Unreadable("not JSON") from e
    if not isinstance(doc, dict) or "format_version" not in doc:
        raise Unreadable("not `terraform show -json` output (no format_version)")
    if doc.get("errored") is True:
        raise Unreadable("the plan errored")
    changes = doc.get("resource_changes") or []
    variables = doc.get("variables") or {}
    if not isinstance(changes, list) or not isinstance(variables, dict):
        raise Unreadable("resource_changes or variables has the wrong shape")
    for rc in changes:
        if not isinstance(rc, dict) or not isinstance(rc.get("address"), str):
            raise Unreadable("a resource change has no address")
        actions = jq(rc, "change", "actions")
        if not isinstance(actions, list) or not actions or not all(isinstance(a, str) for a in actions):
            raise Unreadable("a resource change has no actions")
    return Plan(changes=changes, variables=variables, raw=doc)


def actions(rc: dict) -> list[str]:
    return rc["change"]["actions"]


def is_change(rc: dict) -> bool:
    return actions(rc) not in (["no-op"], ["read"])


# --- The guards: each returns the addresses that break it (empty = pass). -----------------------------------------


def no_delete_or_replace(plan: Plan) -> list[str]:
    """Never destroy or replace anything: no action contains `delete`."""
    return [f"{'+'.join(actions(rc))} {rc['address']}" for rc in plan.changes if "delete" in actions(rc)]


def shopify_network_path(plan: Plan) -> list[str]:
    """Shopify's subnet and IP, the network and the router stay untouched; Shopify's NAT may change only its
    port and logging settings, never its router, IPs or subnets."""
    bad = [
        rc["address"]
        for rc in plan.changes
        if is_change(rc)
        and SHOPIFY_PATH.search(rc["address"])
        and rc.get("type") != "google_compute_router_nat"
    ]
    for rc in plan.changes:
        if rc["address"] != SHOPIFY_NAT:
            continue
        before, after = (jq(rc, "change", side) for side in ("before", "after"))
        if not all(jq_equal(jq(before, k), jq(after, k)) for k in NAT_PINNED):
            bad.append(rc["address"])
    return bad


def shopify_jobs_network(plan: Plan) -> list[str]:
    """crawl-shopify and crawl-retry keep their network (vpc_access) as it is."""
    bad = []
    for rc in plan.changes:
        if rc.get("type") != "google_cloud_run_v2_job":
            continue
        if jq(rc, "change", "before", "name") not in SHOPIFY_JOBS:
            continue
        path = ("template", 0, "template", 0, "vpc_access")
        if not jq_equal(jq(rc, "change", "before", *path), jq(rc, "change", "after", *path)):
            bad.append(rc["address"])
    return bad


def ingest_image_pinned(plan: Plan) -> list[str]:
    """The ingest image is the build's own repository, pinned by digest, and every job's image is a digest."""
    bad = []
    region, project, service, image = (
        jq(plan.variables, name, "value") for name in ("region", "project_id", "service", "ingest_image")
    )
    if not all(isinstance(v, str) and v for v in (region, project, service)):
        bad.append("var.region, var.project_id or var.service (missing from the plan)")
    else:
        repo = re.escape(f"{region}-docker.pkg.dev/{project}/{service}/ingest")
        if not (isinstance(image, str) and re.fullmatch(repo + DIGEST.pattern, image)):
            bad.append("var.ingest_image")
    for rc in plan.changes:
        if rc.get("type") != "google_cloud_run_v2_job" or actions(rc) == ["delete"]:
            continue
        containers = ("template", 0, "template", 0, "containers")
        listed = jq(rc, "change", "after", *containers)
        for i, c in enumerate(listed if isinstance(listed, list) else [None]):
            img = jq(c, "image")
            if img is None and unknown_after(rc, *containers, i, "image"):
                continue  # known only after apply (it comes from var.ingest_image, checked above)
            if not (isinstance(img, str) and DIGEST.search(img)):
                bad.append(rc["address"])
                break
    return bad


def unknown_after(rc: dict, *path) -> bool:
    """after_unknown marks a value Terraform learns only at apply: `true` at the path or at any parent of it."""
    value = jq(rc, "change", "after_unknown")
    for step in path:
        if value is True:
            return True
        if not isinstance(value, list if isinstance(step, int) else dict):
            return False
        value = jq(value, step)
    return value is True


GUARDS: list[tuple[str, str, Callable[[Plan], list[str]]]] = [
    ("no-delete-or-replace", "nothing is destroyed or replaced", no_delete_or_replace),
    (
        "shopify-network-path",
        "Shopify's subnet, IP, NAT IPs and router, the network: unchanged",
        shopify_network_path,
    ),
    ("shopify-jobs-network", "crawl-shopify and crawl-retry keep their network", shopify_jobs_network),
    ("ingest-image-pinned", "the ingest image is pinned by digest", ingest_image_pinned),
]


def run_guards(plan: Plan) -> list[tuple[str, str, list[str]]]:
    return [(name, what, fn(plan)) for name, what, fn in GUARDS]


# --- Scrubbing and rendering ---------------------------------------------------------------------------------------


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def private_strings(plan: Plan) -> list[str]:
    """Every string the plan holds as a variable value, and the egress IPs, long enough to search for."""
    found = _strings({k: jq(v, "value") for k, v in plan.variables.items() if isinstance(v, dict)})
    outputs = {}
    for source in (jq(plan.raw, "planned_values", "outputs"), jq(plan.raw, "output_changes")):
        if isinstance(source, dict):
            outputs.update({k: v for k, v in source.items() if k.endswith("egress_ip")})
    found += _strings(outputs)
    return sorted({s for s in found if len(s) >= MIN_SCRUB_LEN}, key=len, reverse=True)


def scrub_check(text: str, plan: Plan) -> None:
    for s in private_strings(plan):
        if s in text:
            raise Unreadable("the rendered text contains a value from the plan")


def render(plan: Plan) -> str:
    changed = sorted((rc for rc in plan.changes if is_change(rc)), key=lambda rc: rc["address"])
    for rc in changed:
        if not SAFE_ADDRESS.match(rc["address"]):
            raise Unreadable("an address has characters the comment won't carry")
    results = run_guards(plan)
    lines = ["### Guards", ""]
    for name, what, bad in results:
        lines.append(f"- {'FAIL' if bad else 'pass'} `{name}`: {what}")
        lines += [f"  - `{b}`" for b in bad]
    lines += ["", "### Changes", ""]
    lines += [f"- `{'+'.join(actions(rc))}` `{rc['address']}`" for rc in changed] or ["No changes."]
    counts: dict[str, int] = {}
    for rc in changed:
        key = "+".join(actions(rc))
        counts[key] = counts.get(key, 0) + 1
    lines += ["", "### Totals", ""]
    lines.append(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) if counts else "0 changes")
    text = "\n".join(lines) + "\n"
    scrub_check(text, plan)
    return text


def check_text(plan: Plan) -> tuple[str, bool]:
    results = run_guards(plan)
    out = []
    for name, _, bad in results:
        out.append(f"{'FAIL' if bad else 'PASS'} {name}" + (": " + ", ".join(bad) if bad else ""))
    text = "\n".join(out) + "\n"
    scrub_check(text, plan)
    return text, any(bad for _, _, bad in results)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "render"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return UNREADABLE
    command, path = argv
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        plan = load(text)
        if command == "render":
            sys.stdout.write(render(plan))
            return PASS
        out, failed = check_text(plan)
    except (OSError, Unreadable) as e:
        # The reason never quotes the plan: every Unreadable message is fixed text.
        print(
            f"tf_plan_guard: refusing: {e if isinstance(e, Unreadable) else 'cannot read the plan file'}",
            file=sys.stderr,
        )
        return UNREADABLE
    sys.stdout.write(out)
    return FAIL if failed else PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
