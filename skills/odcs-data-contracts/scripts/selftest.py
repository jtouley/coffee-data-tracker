#!/usr/bin/env python3
# Copyright 2026 The Bitol Contributors
# SPDX-License-Identifier: Apache-2.0
"""Check validate_contract.py against an ODCS checkout's own fixtures.

    python selftest.py /path/to/open-data-contract-standard [--version v3.2.0]

Every docs/examples/*/*.odcs.yaml must validate, every src/script/negative-tests/*.odcs.yaml must be
rejected, and no rejection may fall back to jsonschema's raw "Unevaluated properties" message (the
case validate_contract.py exists to explain). Run it in the ODCS repo's CI so this skill breaks loudly
when the standard moves. Exit status 0 when all hold, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

VALIDATOR = Path(__file__).with_name("validate_contract.py")


def run(schema: Path, files: list[Path]) -> list[dict]:
    out = subprocess.run([sys.executable, str(VALIDATOR), "--json", "--schema", str(schema), *map(str, files)],
                         capture_output=True, text=True)
    if out.returncode == 2:
        sys.exit(out.stderr)
    return json.loads(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("odcs_repo", type=Path)
    ap.add_argument("--version", default="v3.2.0")
    a = ap.parse_args()
    root = a.odcs_repo
    schema = root / "schema" / f"odcs-json-schema-{a.version}.json"
    examples = sorted((root / "docs" / "examples").glob("*/*.odcs.yaml"))
    negatives = sorted((root / "src" / "script" / "negative-tests").glob("*.odcs.yaml"))
    if not (schema.exists() and examples and negatives):
        sys.exit(f"not an ODCS checkout, or no {schema.name}: {root}")

    problems = []
    for r in run(schema, examples):
        if not r["valid"]:
            problems.append(f"example rejected: {r['file']}: {r['errors'][0]['message']}")
    for r in run(schema, negatives):
        if r["valid"]:
            problems.append(f"negative test accepted: {r['file']}")
        elif any(e["message"].startswith("Unevaluated properties") for e in r["errors"]):
            problems.append(f"unexplained error: {r['file']}: {r['errors'][0]['message']}")

    for p in problems:
        print(p)
    print(f"{len(examples)} examples, {len(negatives)} negative tests, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
