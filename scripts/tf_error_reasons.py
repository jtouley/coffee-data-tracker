#!/usr/bin/env python3
"""Why a Terraform step failed, as far as an allow-list can say: for each error in its log, the denied permission's
name and the reason's class. Printed under the guard's fixed summary (`tf_plan_guard.py errors`) by the apply
workflow, so a missing IAM permission is named where the failure is, not only in Cloud Audit Logs.

    python3 -I scripts/tf_error_reasons.py LOG    # `-` reads stdin; always exit 0

The repository is public and Terraform's messages can quote values, so nothing is copied from the log except:

- a permission's name: only where a message says it is the permission (PERMISSION_CONTEXTS), only if it has a
  permission's shape (PERMISSION, at least MIN_PARTS parts), only if it starts with a Google service this project
  uses (SERVICES), and only if it neither holds a TF_VAR_* value nor is a piece of one;
- the error's resource address, by the guard's own rule (tf_plan_guard.Scrubber: Terraform's address syntax with
  plain keys, no TF_VAR_* value), the address the guard's summary already shows. Without the guard beside this
  file, no address is shown.

Everything else printed is fixed text from this file: the reasons in REASONS, chosen by an HTTP status or a status
word in the message, and the two hints. A message that matches nothing prints nothing: no resource name, value,
email, project number or path, however the message is worded.

Standard library only; no network, no child process, no file but LOG and the guard read.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

# A Google IAM permission: service.resource.verb, sometimes with one more part (logging.notificationRules.create,
# artifactregistry.repositories.downloadArtifacts). Letters and dots only: no digits, hyphens, slashes or @.
PERMISSION = re.compile(r"^[a-z][a-zA-Z]*(\.[a-zA-Z]+){1,3}$")
MIN_PARTS = (
    3  # every IAM permission is at least service.resource.verb; a two-part token (a domain, say) is refused
)
_NAME = r"([A-Za-z.]{3,120})"
# The only places a name is read from, each a wording Google's APIs use to say which permission was missing. The
# name must be followed by its closing quote or by whitespace, so a longer token is never cut down to a name.
PERMISSION_CONTEXTS = tuple(
    re.compile(p)
    for p in (
        rf"""[Pp]ermission\s+(['"]){_NAME}\1\s+denied\b""",  # Permission 'x.y.z' denied on resource ...
        rf"""\bRequired\s+(['"]){_NAME}\1\s+permission\b""",  # Required 'compute.x.get' permission for ...
        rf"""(")permission"\s*:\s*"{_NAME}\1""",  # ErrorInfo metadata: "permission": "x.y.z"
        rf"""\bdoes not have\s+(){_NAME}\s+access\b""",  # Cloud Storage: ... does not have storage.objects.get access
    )
)
# The first part of a name: the services whose permissions the two Terraform roots grant or whose APIs they enable
# (src/terraform, src/terraform/bootstrap), and Cloud SQL for the admin. A name under any other first word is not
# shown, so a dotted piece of a value (mail.example.org, jane.q.public) never passes for a permission. A denial for a
# service missing here reads "403 permission denied" with no name: add the service when that happens.
SERVICES = frozenset(
    {
        "artifactregistry",
        "billing",
        "billingbudgets",
        "cloudfunctions",
        "cloudscheduler",
        "cloudsql",
        "compute",
        "iam",
        "iamcredentials",
        "logging",
        "monitoring",
        "pubsub",
        "resourcemanager",
        "run",
        "secretmanager",
        "serviceusage",
        "storage",
        "sts",
        "vpcaccess",
    }
)
MAX_PERMISSIONS = 5  # per error
MIN_SCRUB_LEN = 6  # as the guard: a shorter value would match names by accident

DENIED = "403 permission denied"
QUOTA = "quota"
API_OFF = "API not enabled"
# The whole vocabulary of reasons, by HTTP status. Any other status prints nothing.
REASONS = {
    400: "400 invalid argument",
    403: DENIED,
    404: "404 not found",
    409: "409 already exists",
    429: QUOTA,
}
SERVER = "5xx server error"
STATUS = re.compile(r"\b(?:googleapi: )?Error ([1-5][0-9]{2})\b|\bHTTP response code ([1-5][0-9]{2})\b")
# gRPC status names and googleapi reason words, as whole tokens.
DENIED_WORD = re.compile(r"\b(?:PERMISSION_DENIED|permission_denied|IAM_PERMISSION_DENIED|forbidden)\b")
WORDS = (
    (
        re.compile(
            r"\b(?:RESOURCE_EXHAUSTED|quotaExceeded|rateLimitExceeded|userRateLimitExceeded|[Qq]uota exceeded)\b"
        ),
        QUOTA,
    ),
    (re.compile(r"\b(?:accessNotConfigured|SERVICE_DISABLED)\b"), API_OFF),
    (DENIED_WORD, DENIED),
    (re.compile(r"\b(?:NOT_FOUND|notFound)\b"), REASONS[404]),
    (re.compile(r"\b(?:ALREADY_EXISTS|alreadyExists)\b"), REASONS[409]),
    (re.compile(r"\b(?:INVALID_ARGUMENT|badRequest)\b"), REASONS[400]),
)
ORDER = (QUOTA, API_OFF, DENIED, REASONS[404], REASONS[409], REASONS[400], SERVER)

HINT = (
    "if the bootstrap on main grants this, run the by-hand bootstrap block (docs/app/RUNNING.md) and run this "
    "workflow again from main"
)
UNNAMED = (
    "no permission was recognised in Terraform's message: Cloud Audit Logs name it (the denied call's entry for the "
    "account this job runs as)"
)
HEADER = "Why (permission names and a fixed list of reasons only; anything else in Terraform's message is not shown):"


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


_GUARD: list = []  # the guard's module once loaded, or None once it couldn't be


def _guard_scrubber(private: list[str]):
    """The guard's Scrubber, loaded once from the file beside this one (`python3 -I` puts no folder on the path), under
    a name of its own; None if it isn't there or can't be loaded, and then no address is shown."""
    if not _GUARD:
        module = None
        name = "_tf_error_reasons_guard"
        try:
            path = Path(__file__).resolve().with_name("tf_plan_guard.py")
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module  # dataclasses looks its module up while the guard is loading
                spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - no guard, no address
            sys.modules.pop(name, None)
            module = None
        _GUARD.append(module)
    try:
        return _GUARD[0].Scrubber(private) if _GUARD[0] is not None else None
    except Exception:  # noqa: BLE001
        return None


def blocks(log: str) -> list[list[str]]:
    """One list of lines per error: from each `Error: ` line to the end of that diagnostic (its closing `╵`, a
    `Warning: ` line or the next `Error: `), so nothing of a warning that follows is read as the error's."""
    out: list[list[str]] = []
    inside = False
    for line in log.splitlines():
        if re.match(r"^\W*Error: ", line):
            out.append([line])
            inside = True
        elif re.match(r"^\W*Warning: ", line) or line.strip() == "╵":
            inside = False
        elif inside:
            out[-1].append(line)
    return out


def permissions(text: str, private: set[str]) -> list[str]:
    """The permission names a message gives as denied, in order, each once: only from PERMISSION_CONTEXTS, only in
    PERMISSION's shape, never one that holds a private value."""
    found: list[str] = []
    for context in PERMISSION_CONTEXTS:
        for m in context.finditer(text):
            name = m.group(2)
            if not PERMISSION.fullmatch(name) or name.count(".") < MIN_PARTS - 1 or name in found:
                continue
            if name.split(".", 1)[0] not in SERVICES:
                continue
            n = _normalize(name)
            if any(p in n or n in p for p in private):  # holds a private value, or is a piece of one
                continue
            found.append(name)
    return found[:MAX_PERMISSIONS]


def reasons(text: str) -> list[str]:
    """The reasons a message matches, from the fixed vocabulary, in ORDER."""
    seen: set[str] = set()
    for m in STATUS.finditer(text):
        code = int(m.group(1) or m.group(2))
        if code in REASONS:
            seen.add(REASONS[code])
        elif 500 <= code <= 599:
            seen.add(SERVER)
    for pattern, reason in WORDS:
        if pattern.search(text):
            seen.add(reason)
    # A quota refusal or a disabled API comes as a 403 too: neither is a missing permission.
    if (QUOTA in seen or API_OFF in seen) and not DENIED_WORD.search(text):
        seen.discard(DENIED)
    return [r for r in ORDER if r in seen]


def reasons_text(log: str, env: dict[str, str]) -> str:
    """The lines to print for a failed step's log; empty when no error matches anything."""
    values = [v for k, v in env.items() if k.startswith("TF_VAR_")]
    private = {n for n in map(_normalize, values) if len(n) >= MIN_SCRUB_LEN}
    scrub = _guard_scrubber(values)
    out: list[str] = []
    denied = unnamed = False
    for i, block in enumerate(blocks(log), 1):
        text = "\n".join(block)
        names = permissions(text, private)
        why = reasons(text)
        if names and DENIED not in why:
            why = [
                r for r in ORDER if r in {DENIED, *why}
            ]  # a named denied permission is a 403 by definition
        if not why:
            continue
        address = None
        if scrub is not None:
            for line in block:
                m = re.match(r"^\W*with (\S+),$", line)
                if m:
                    try:
                        address = scrub.printable(m[1])
                    except Exception:  # noqa: BLE001 - the guard refuses it: not shown here either
                        address = None
                    break
        item = f"  error {i}" + (f", {address}" if address else "") + ": " + ", ".join(why)
        if names:
            item += ": " + ", ".join(names)
            denied = True
        elif DENIED in why and DENIED_WORD.search(text):  # said to be a denial, not only a status
            unnamed = True
        out.append(item)
    if not out:
        return ""
    if unnamed:
        out.append(f"  {UNNAMED}")
    if denied or unnamed:
        out.append(f"  {HINT}")
    return "\n".join([HEADER, *out]) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 -I scripts/tf_error_reasons.py LOG", file=sys.stderr)
        return 0
    try:
        if argv[0] == "-":
            log = sys.stdin.read()
        else:
            with open(argv[0], encoding="utf-8", errors="replace") as f:
                log = f.read()
        sys.stdout.write(reasons_text(log, dict(os.environ)))
    except Exception:  # noqa: BLE001 - this only adds a hint: it never fails the step, and never quotes the log
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
