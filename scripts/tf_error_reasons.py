#!/usr/bin/env python3
"""Why a Terraform step failed, as far as an allow-list can say: for each error in its log, the denied permission's
name and the reason's class. Printed under the guard's fixed summary (`tf_plan_guard.py errors`) by the apply
workflow, so a missing IAM permission is named where the failure is, not only in Cloud Audit Logs.

    python3 -I scripts/tf_error_reasons.py LOG    # `-` reads stdin; always exit 0

The repository is public and Terraform's messages can quote values, so nothing is copied from the log except:

- a permission's name: only where a message says it is the permission (PERMISSION_CONTEXTS), only if it has a
  permission's shape (PERMISSION, at least MIN_PARTS parts), and only if it holds no TF_VAR_* value;
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
MAX_PERMISSIONS = 5  # per error
MIN_SCRUB_LEN = 6  # as the guard: a shorter value would match names by accident

DENIED = "403 permission denied"
QUOTA = "quota"
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
WORDS = (
    (re.compile(r"\b(?:RESOURCE_EXHAUSTED|quotaExceeded|rateLimitExceeded|[Qq]uota exceeded)\b"), QUOTA),
    (re.compile(r"\b(?:PERMISSION_DENIED|permission_denied|IAM_PERMISSION_DENIED)\b"), DENIED),
    (re.compile(r"\b(?:NOT_FOUND|notFound)\b"), REASONS[404]),
    (re.compile(r"\b(?:ALREADY_EXISTS|alreadyExists)\b"), REASONS[409]),
    (re.compile(r"\b(?:INVALID_ARGUMENT|badRequest)\b"), REASONS[400]),
)
ORDER = (QUOTA, DENIED, REASONS[404], REASONS[409], REASONS[400], SERVER)

HINT = (
    "if the bootstrap on main grants this, run the by-hand bootstrap block (docs/app/RUNNING.md) and run this "
    "workflow again from main"
)
UNNAMED = (
    "Terraform's message names no permission: Cloud Audit Logs do (the denied call's entry for the account this "
    "job runs as)"
)
HEADER = "Why (permission names and a fixed list of reasons only; anything else in Terraform's message is not shown):"


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def _guard_scrubber(private: list[str]):
    """The guard's Scrubber, loaded from the file beside this one (`python3 -I` puts no folder on the path); None
    if it isn't there or can't be loaded, and then no address is shown."""
    try:
        path = Path(__file__).resolve().with_name("tf_plan_guard.py")
        spec = importlib.util.spec_from_file_location("tf_plan_guard", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses looks its module up while the guard is loading
        spec.loader.exec_module(module)
        return module.Scrubber(private)
    except Exception:  # noqa: BLE001 - no guard, no address
        return None


def blocks(log: str) -> list[list[str]]:
    """One list of lines per error: from each `Error: ` line to the next."""
    out: list[list[str]] = []
    for line in log.splitlines():
        if re.match(r"^\W*Error: ", line):
            out.append([line])
        elif out:
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
            if any(p in _normalize(name) for p in private):
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
    if QUOTA in seen:  # a quota refusal comes as a 403 or a 429: it isn't a missing permission
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
                DENIED,
                *[r for r in why if r != QUOTA],
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
        elif DENIED in why:
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
