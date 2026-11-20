#!/usr/bin/env python3
"""Before an apply: does the apply account hold what the bootstrap on main grants it? (terraform-apply.yml)

The bootstrap root (src/terraform/bootstrap/) is applied by the owner, by hand, from Cloud Shell. A merged bootstrap
change that isn't applied yet leaves the apply account short of a permission, and the GitHub apply then fails partway
with a 403 whose details only the audit logs show. This reads, from main's bootstrap iam.tf, every permission of the
custom roles granted to the apply account on the project without a condition, and asks Resource Manager's
`projects.testIamPermissions`, as the apply account itself, which of them it holds.

    python3 -I scripts/tf_bootstrap_preflight.py permissions IAM_TF        # the permissions to test, one per line
    printf '%s' "$token" | python3 -I scripts/tf_bootstrap_preflight.py check IAM_TF [--project ID]
                                                    # exit 0 all held, 1 some missing, 2 the test itself failed

`check` reads the apply account's access token from stdin (never an argument or the environment, so it is in no
process listing) and prints only permission names and fixed text. The project defaults to `variable "project_id"`'s
default in the bootstrap's variables.tf, the project its grants are on. Standard library only.

Not tested, because a project-level test can't see them:
- conditional grants (a `condition` block), such as grants limited to named buckets or instances: the project-level
  answer for those permissions is "not held", whether the grant is applied or not;
- resource-level grants (google_storage_bucket_iam_member, google_artifact_registry_repository_iam_member, ...), such
  as tfApplyRepository on the image repository and tfApplyBucketAdmin on the staging bucket;
- acts-as (roles/iam.serviceAccountUser on each runtime account) and the billing account's role;
- predefined roles (roles/...): their permissions are Google's, not this repository's.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from pathlib import Path

# Resource Manager's documented limit on permissions per testIamPermissions call.
BATCH = 100
API = "https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions"
TIMEOUT = 30

AHEAD = (
    "the bootstrap on main is ahead of what's applied: run the by-hand bootstrap block in docs/app/RUNNING.md, "
    "then re-run this workflow from main"
)

# `terraform fmt` puts a top-level block's closing brace in column 0.
RESOURCE = re.compile(r'^resource "(\w+)" "(\w+)" \{\n(.*?)^\}', re.M | re.S)
CUSTOM_ROLE_REF = re.compile(r"\bgoogle_project_iam_custom_role\.(\w+)\.id\b")
PERMISSION = re.compile(r"^[a-z][a-zA-Z0-9]*(\.[a-zA-Z0-9]+){2,}$")
APPLY_MEMBER = re.compile(r"^\s*member\s*=\s*(local\.apply_member|\"serviceAccount:\$\{google_service_account\.tf_apply\.email\}\")\s*$", re.M)


class ParseError(ValueError):
    """iam.tf has a shape this reader doesn't know: fail closed rather than test less."""


def _strip_comments(code: str) -> str:
    """Drop `#` and `//` comments outside double-quoted strings (a comment can say `condition {`)."""
    out = []
    for line in code.splitlines():
        in_str = escaped = False
        cut = len(line)
        for i, ch in enumerate(line):
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "#" or line.startswith("//", i):
                cut = i
                break
        out.append(line[:cut].rstrip())
    return "\n".join(out) + "\n"


def _custom_roles(code: str) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {}
    for rtype, name, body in RESOURCE.findall(code):
        if rtype != "google_project_iam_custom_role":
            continue
        m = re.search(r"^\s*permissions\s*=\s*\[([^\]]*)\]", body, re.M)
        if not m:
            raise ParseError(f"custom role {name}: no literal permissions list")
        perms = re.findall(r'"([^"]*)"', m.group(1))
        leftover = re.sub(r'"[^"]*"|[\s,]', "", m.group(1))
        if leftover or not perms or not all(PERMISSION.match(p) for p in perms):
            raise ParseError(f"custom role {name}: permissions aren't a list of literal permission names")
        roles[name] = perms
    declared = len(re.findall(r'^resource "google_project_iam_custom_role"', code, re.M))
    if len(roles) != declared:
        raise ParseError(f"read {len(roles)} of {declared} custom roles")
    return roles


def project_grants(code: str) -> list[str]:
    """Names of the custom roles granted to the apply account on the project with no condition, in file order."""
    code = _strip_comments(code)
    roles = _custom_roles(code)
    granted: list[str] = []
    for rtype, name, body in RESOURCE.findall(code):
        if rtype != "google_project_iam_member" or not APPLY_MEMBER.search(body):
            continue
        if re.search(r"^\s*condition\s*\{", body, re.M):
            continue  # conditional: a project-level test can't see it (module docstring)
        refs = CUSTOM_ROLE_REF.findall(body)
        role_line = re.search(r"^\s*role\s*=\s*(.+)$", body, re.M)
        if role_line is None:
            raise ParseError(f"google_project_iam_member.{name}: no role")
        role = role_line.group(1).strip()
        if not refs and not (role == "each.value" or re.fullmatch(r'"roles/[\w.]+"', role)):
            # a role this reader can't place (a local, a variable): it might be a custom one
            raise ParseError(f"google_project_iam_member.{name}: role {role} isn't a predefined role or a custom role")
        for ref in refs:
            if ref not in roles:
                raise ParseError(f"google_project_iam_member.{name}: no custom role {ref} in this file")
            if ref not in granted:
                granted.append(ref)
    return granted


def permissions(code: str) -> list[str]:
    """Every permission to test, sorted and unique."""
    roles = _custom_roles(_strip_comments(code))
    return sorted({p for ref in project_grants(code) for p in roles[ref]})


def batches(items: Iterable[str], size: int = BATCH) -> Iterator[list[str]]:
    batch: list[str] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def missing_message(missing: Iterable[str]) -> str:
    names = ", ".join(sorted(missing))
    return (
        f"::error::The apply account lacks {names} on the project, which the bootstrap on main grants it: {AHEAD} "
        "(a new run: re-runs of this one never apply). Nothing was applied."
    )


def project_from(variables_tf: Path) -> str:
    code = _strip_comments(variables_tf.read_text())
    m = re.search(r'^variable "project_id" \{\n(.*?)^\}', code, re.M | re.S)
    d = m and re.search(r'^\s*default\s*=\s*"([a-z][a-z0-9-]{4,28}[a-z0-9])"\s*$', m.group(1), re.M)
    if not d:
        raise ParseError(f"no literal project_id default in {variables_tf}")
    return d.group(1)


def held_permissions(project: str, token: str, perms: list[str]) -> set[str]:
    """The subset of `perms` the token's account holds on the project. Raises RuntimeError with fixed text."""
    held: set[str] = set()
    for batch in batches(perms):
        req = urllib.request.Request(
            API.format(project=project),
            data=json.dumps({"permissions": batch}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("no response") from None
        except ValueError:
            raise RuntimeError("an unreadable response") from None
        got = body.get("permissions", []) if isinstance(body, dict) else None
        if not isinstance(got, list) or not all(isinstance(p, str) for p in got):
            raise RuntimeError("an unreadable response")
        held |= set(got) & set(batch)
    return held


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("permissions")
    p.add_argument("iam_tf", type=Path)
    c = sub.add_parser("check")
    c.add_argument("iam_tf", type=Path)
    c.add_argument("--project")
    args = ap.parse_args(argv)

    try:
        code = args.iam_tf.read_text()
        perms = permissions(code)
        if args.cmd == "check":
            project = args.project or project_from(args.iam_tf.with_name("variables.tf"))
    except (OSError, ParseError) as e:
        print(f"::error::Couldn't read the bootstrap's grants ({e}). Nothing was applied.")
        return 2
    if args.cmd == "permissions":
        print("\n".join(perms))
        return 0

    token = sys.stdin.read().strip()
    if not token:
        print("::error::No access token on stdin. Nothing was applied.")
        return 2
    if not perms:
        print("No unconditioned project-level custom-role grants to the apply account: nothing to test.")
        return 0
    try:
        held = held_permissions(project, token, perms)
    except RuntimeError as e:
        print(f"::error::Couldn't test the apply account's permissions on {project} ({e}). Nothing was applied.")
        return 2
    missing = [x for x in perms if x not in held]
    if missing:
        print(missing_message(missing))
        return 1
    print(f"The apply account holds all {len(perms)} permissions its project-level bootstrap custom roles grant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
