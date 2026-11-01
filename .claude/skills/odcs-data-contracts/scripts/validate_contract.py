#!/usr/bin/env python3
# Copyright 2026 The Bitol Contributors
# SPDX-License-Identifier: Apache-2.0
"""Validate ODCS data contracts against the official JSON schema and explain failures.

    python validate_contract.py contract.odcs.yaml [more.odcs.yaml ...]
        [--schema PATH] [--ref main|dev|<sha>] [--json]

The schema is the one matching each contract's `apiVersion`, found in this order:
  1. --schema PATH (or the ODCS_SCHEMA environment variable);
  2. a file named odcs-json-schema-<apiVersion>.json in a folder above the contract or the
     working directory, searched to a depth of three (an ODCS checkout, or a project that
     vendors the schema);
  3. a download from github.com/bitol-io/open-data-contract-standard at --ref (default main),
     cached under ~/.cache/odcs-schemas.
The schema used is printed with its SHA-256, because the rolling vX.Y.Z files get silent fixes.

Why not print jsonschema's errors as they are: ODCS uses draft 2019-09 with oneOf/anyOf and
`unevaluatedProperties`, so a single wrong value often surfaces as "Unevaluated properties are
not allowed ('name', ... were unexpected)" on the parent, which names the wrong culprit. This
script walks the nested errors, keeps the most specific ones, and re-checks each "unexpected"
key against the schema's own definitions of that key to say what is actually wrong with it.

Exit status: 0 when every contract is valid (warnings allowed), 1 when any is invalid,
2 on usage errors (file missing, unparseable YAML, no schema).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

try:
    import jsonschema
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("Needs PyYAML and jsonschema>=4.18: pip install pyyaml jsonschema")

RAW = "https://raw.githubusercontent.com/bitol-io/open-data-contract-standard/{ref}/schema/odcs-json-schema-{version}.json"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "odcs-schemas"

# Fields the standard keeps valid but has deprecated. (path regex, message)
DEPRECATIONS = [
    (r"^\$\.dataProduct$", "`dataProduct` is deprecated since v3.1.0; link the contract from the data product (ODPS) instead."),
    (r"^\$\.slaDefaultElement$", "`slaDefaultElement` is deprecated since v3.1.0 and removed in v4.0.0; set `element` on each slaProperties entry."),
    (r"\.quality\[\d+\]\.rule$", "`quality[].rule` is deprecated; use `metric`."),
]
# Keys that look plausible but are not ODCS fields, and what to use instead.
NOT_ODCS = {
    "element": "quality rules attach to the schema object or property they sit under; there is no `element` key (SLA entries are the exception).",
    "stakeholders": "renamed `team` in v3.",
    "columns": "use `properties` under a schema object.",
    "fields": "use `properties` under a schema object.",
    "type": "schema properties use `logicalType` (and `physicalType`); `type` belongs to quality rules, servers and authoritativeDefinitions.",
    "dataType": "use `logicalType` (portable) and `physicalType` (platform-specific).",
    "nullable": "use `required: true` for non-null; the default is nullable.",
    "rule": "deprecated; use `metric` (with `type: library`), or `type: sql` + `query`, or `type: custom` + `implementation`.",
    "pii": "use `classification` (e.g. `restricted`) and/or `tags`, or a customProperty.",
}
SPECIFIC = {"type": 0, "enum": 0, "const": 0, "pattern": 0, "required": 1, "minItems": 1, "maxItems": 1,
            "uniqueItems": 1, "minimum": 1, "maximum": 1, "format": 1, "not": 2, "additionalProperties": 3,
            "unevaluatedProperties": 4}


def load_yaml(path: Path):
    text = path.read_text(encoding="utf-8")
    # ODCS dates are strings; keep YAML from turning 2024-01-01 into a date object.
    class Loader(yaml.SafeLoader):
        pass
    Loader.yaml_implicit_resolvers = {
        k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
        for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    return yaml.load(text, Loader=Loader)


def find_schema(version: str, starts: list[Path], ref: str) -> Path:
    name = f"odcs-json-schema-{version}.json"
    seen = set()
    for start in starts:
        for folder in [start, *start.parents]:
            if folder in seen:
                continue
            seen.add(folder)
            for depth in ("", "*/", "*/*/", "*/*/*/"):
                hits = sorted(p for p in folder.glob(depth + name) if ".git" not in p.parts)
                if hits:
                    return hits[0]
    cached = CACHE / ref / name
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        url = RAW.format(ref=ref, version=version)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                cached.write_bytes(r.read())
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"error: no local {name} and could not download {url}: {e}\n"
                             f"Pass --schema PATH to a copy of the schema.") from e
    return cached


def leaves(err):
    if err.context:
        for c in err.context:
            yield from leaves(c)
    else:
        yield err


def depth(err) -> int:
    return len(err.absolute_path)


def key_definitions(schema) -> dict[str, list]:
    """Every subschema the ODCS schema gives for a property name, anywhere in the schema."""
    defs: dict[str, list] = {}

    def walk(node):
        if isinstance(node, dict):
            for k, sub in (node.get("properties") or {}).items():
                defs.setdefault(k, []).append(sub)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema)
    return defs


def explain_unexpected(key, value, defs, validator) -> str:
    if key not in defs:
        hint = NOT_ODCS.get(key)
        return f"`{key}` is not an ODCS field here" + (f": {hint}" if hint else ".")
    problems = []
    for sub in defs[key]:
        errs = list(validator.evolve(schema=sub).iter_errors(value))
        if not errs:
            return ""  # valid by some definition: a side effect of another error, not a cause
        problems.append(jsonschema.exceptions.best_match(errs).message)
    return f"`{key}` has an invalid value: {problems[0]}"


def readable(e) -> str:
    """jsonschema's message, rewritten where the raw one dumps whole subschemas."""
    if e.validator == "not" and isinstance(e.validator_value, dict) and "required" in e.validator_value:
        keys = ", ".join(f"'{k}'" for k in e.validator_value["required"])
        return f"must not set {keys} here"
    if e.validator in ("oneOf", "anyOf"):
        return f"matches {'more than one' if 'valid under each' in e.message else 'none'} of the allowed shapes"
    return e.message


def diagnose(doc, validator, defs) -> list[tuple[str, str]]:
    raw = [leaf for top in validator.iter_errors(doc) for leaf in leaves(top)]
    specific = [e for e in raw if e.validator not in ("unevaluatedProperties", "additionalProperties")]
    # A "matches more than one shape" error is a side effect when the same object has a real error.
    specific = [e for e in specific if not (e.validator in ("oneOf", "anyOf") and any(
        o is not e and o.json_path == e.json_path and o.validator not in ("oneOf", "anyOf") for o in specific))]
    out: dict[tuple[str, str], object] = {}  # (path, message) -> the `required` list it came from

    # Alternatives of an anyOf/oneOf that each want a different key ("mustBe is required",
    # "mustNotBe is required", ...) are noise when a more specific error sits deeper.
    deepest = max((depth(e) for e in specific), default=-1)
    for e in sorted(specific, key=lambda e: (-depth(e), SPECIFIC.get(e.validator, 2))):
        if e.validator == "required" and depth(e) < deepest and any(
            depth(o) > depth(e) and list(o.absolute_path)[: depth(e)] == list(e.absolute_path) for o in specific
        ):
            continue
        out[(e.json_path, readable(e))] = tuple(e.validator_value) if e.validator == "required" else None

    for e in raw:
        if e.validator not in ("unevaluatedProperties", "additionalProperties"):
            continue
        extra = re.findall(r"'([^']+)'", e.message.split("(")[-1]) if "(" in e.message else []
        explained_below = any(p == e.json_path or p.startswith(e.json_path + ".") or p.startswith(e.json_path + "[")
                              for p, _ in out)
        for key in extra:
            if not isinstance(e.instance, dict) or key not in e.instance:
                continue
            path = f"{e.json_path}.{key}"
            if any(p == path or p.startswith(path + ".") or p.startswith(path + "[") for p, _ in out):
                continue  # already explained by a specific error
            why = explain_unexpected(key, e.instance[key], defs, validator)
            if not why and not explained_below:
                # The value is fine wherever this key is defined; it just isn't allowed here.
                hint = NOT_ODCS.get(key)
                why = f"`{key}` is not allowed here" + (f": {hint}" if hint else ".")
            if why:
                out[(path, why)] = None
    if not out and raw:  # nothing more specific found: fall back to jsonschema's own pick
        b = jsonschema.exceptions.best_match(raw)
        out[(b.json_path, readable(b))] = None

    # Missing keys at one path read better as one line. Keys from the same `required` list are
    # all needed; keys from different lists are alternatives (one branch of an anyOf/oneOf).
    grouped: dict[str, list[tuple[str, object]]] = {}
    result = []
    for (path, msg), req in out.items():
        m = re.fullmatch(r"'(.+)' is a required property", msg)
        if m:
            grouped.setdefault(path, []).append((m.group(1), req))
        else:
            result.append((path, msg))
    for path, items in grouped.items():
        keys = [k for k, _ in items]
        if len(keys) == 1:
            msg = f"'{keys[0]}' is a required property"
        elif len({r for _, r in items}) == 1:
            msg = "missing required: " + ", ".join(f"'{k}'" for k in keys)
        else:
            msg = "needs one of: " + ", ".join(f"'{k}'" for k in keys)
        result.append((path, msg))
    return sorted(result, key=lambda pm: pm[0])


def warnings(doc, path: Path) -> list[tuple[str, str]]:
    found = []

    def walk(node, jp):
        for rx, msg in DEPRECATIONS:
            if re.search(rx, jp):
                found.append((jp, msg))
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{jp}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{jp}[{i}]")
    walk(doc, "$")
    if isinstance(doc, dict) and isinstance(doc.get("team"), list):
        found.append(("$.team", "a list under `team` is the pre-v3.1 structure, deprecated and removed in v4; "
                                "use `team: {name, members: [...]}`."))
    if not path.name.endswith(".odcs.yaml"):
        found.append(("file", f"name it *.odcs.yaml so editors and tools recognise it as ODCS ({path.name})."))
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("contracts", nargs="+", type=Path)
    ap.add_argument("--schema", type=Path, default=os.environ.get("ODCS_SCHEMA"))
    ap.add_argument("--ref", default="main", help="git ref of bitol-io/open-data-contract-standard to download from")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    report, status = [], 0
    for path in a.contracts:
        if not path.exists():
            print(f"error: {path} not found", file=sys.stderr)
            return 2
        try:
            doc = load_yaml(path)
        except yaml.YAMLError as e:
            print(f"error: {path} is not valid YAML: {e}", file=sys.stderr)
            return 2
        version = doc.get("apiVersion") if isinstance(doc, dict) else None
        schema_path = a.schema or find_schema(
            version if isinstance(version, str) and re.fullmatch(r"v\d+\.\d+\.\d+", version) else "latest",
            [path.resolve().parent, Path.cwd()], a.ref)
        data = Path(schema_path).read_bytes()
        schema = json.loads(data)
        validator = jsonschema.validators.validator_for(schema)(schema)
        errors = diagnose(doc, validator, key_definitions(schema))
        warns = warnings(doc, path)
        status = status or (1 if errors else 0)
        report.append({"file": str(path), "apiVersion": version, "schema": str(schema_path),
                       "schemaSha256": hashlib.sha256(data).hexdigest()[:12], "valid": not errors,
                       "errors": [{"path": p, "message": m} for p, m in errors],
                       "warnings": [{"path": p, "message": m} for p, m in warns]})

    if a.json:
        print(json.dumps(report, indent=2))
        return status
    for r in report:
        head = "VALID" if r["valid"] else f"INVALID ({len(r['errors'])} error{'s' * (len(r['errors']) != 1)})"
        print(f"{r['file']}: {head}  [apiVersion {r['apiVersion']}; schema {r['schema']} sha256:{r['schemaSha256']}]")
        for e in r["errors"]:
            print(f"  ERROR   {e['path']}: {e['message']}")
        for w in r["warnings"]:
            print(f"  WARNING {w['path']}: {w['message']}")
    return status


if __name__ == "__main__":
    sys.exit(main())
