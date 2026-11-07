"""Rule settings: code defines the rule types, the admin's settings configure them (docs/design/admin-registry.md
§2.3-2.4).

A rule raises **flags** about a product in the build (dbt `flag_<id>`, collected in the ops table `mart_flags`). Each
flag has a **polarity**: `supports` a removal, `doubts` one, or argues to `restore` a product. Polarity is never
implied: a doubt is never counted as a vote to remove (the review's M1).

- **Readiness** (`readiness.removal`): a removal is ready only with `supports` flags from `supports_min` distinct
  families, no open `doubts` or coverage problem (dbt), and a live check of the store (the admin).
- **The guard** (`guard`): in mode `remove_unless_doubted`, a product the classifier removes from the served
  release is kept as coffee while a `doubted_by` flag doubts it, for at most `max_weeks`; `classifier_guard` fails a
  build holding more than `max_held`. It ships `off`: turning it on changes public output, so it needs its
  evaluation (eval/guard/) first, like any rule that decides.

Adding a rule type is one `RuleType` entry here plus its model, `dbt/models/flags/flag_<id>.sql`, which returns one
row per flag: coffee_id, roaster_id, direction ('' for a rule with one), evidence (JSON text, shown on the Case
page) and evidence_key (the evidence without the product's identity, so equal evidence groups). The settings
schema, the defaults, the admin's form and dbt's var come from the entry.

Settings travel in the admin's snapshot (coffeedata.decisions), so a build reads them without the database; a build
without a snapshot uses DEFAULTS.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
POLARITIES = ("supports", "doubts", "restore")
FAMILIES = ("classifier", "catalog", "listing")
GUARD_MODES = ("off", "remove_unless_doubted")
PROPOSALS = ("exclude", "include")
NAME = re.compile(r"[a-z0-9][a-z0-9 &-]{0,59}")  # a listed name reaches dbt's SQL: nothing to escape


class SettingsError(ValueError):
    """Settings the deployed code can't read: every problem, one per line."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Param:
    kind: type  # int or list (of str)
    default: Any
    lo: int = 1
    hi: int = 10_000


@dataclass(frozen=True)
class RuleType:
    id: str
    family: str
    doc: str
    polarity: dict[str, str]  # direction -> default polarity; '' for a rule with one direction
    params: dict[str, Param] = field(default_factory=dict)
    proposes: str | None = None  # the decision the Queue offers for its subjects


RULE_TYPES: dict[str, RuleType] = {
    r.id: r
    for r in (
        RuleType(
            "verdict_flip", "classifier",
            "The classifier's answer differs from the served release: `removed` (it was public, now isn't coffee) "
            "or `added` (seen before the served release, not public there, now coffee).",
            {"added": "supports", "removed": "restore"},
        ),
        RuleType(
            "shared_body", "catalog",
            "The description, or a paragraph of it of at least min_chars characters, is the same on min_products "
            "or more of the roaster's products: pasted text, which a classifier can read as the product's own.",
            {"": "doubts"}, {"min_products": Param(int, 3, 2, 1000), "min_chars": Param(int, 60, 10, 2000)},
        ),
        RuleType(
            "non_coffee_type", "listing",
            "The store's own product type names a kind of product that isn't coffee (a whole word, singular or "
            "plural).",
            {"": "supports"}, {"types": Param(list, ["drink mix", "nuts", "books", "parts"], 1, 50)}, "exclude",
        ),
    )
}  # fmt: skip

DEFAULTS: dict = {
    "schema": SCHEMA_VERSION,
    "rules": [
        {"id": r.id, "family": r.family,
         "polarity": r.polarity[""] if list(r.polarity) == [""] else dict(r.polarity),
         **({"params": {k: p.default for k, p in r.params.items()}} if r.params else {}),
         **({"proposes": r.proposes} if r.proposes else {})}
        for r in RULE_TYPES.values()
    ],
    "readiness": {"removal": {"supports_min": 2, "distinct_families": True, "live_check": "required",
                              "blocked_by": ["doubts", "coverage"]}},
    "guard": {"mode": "off", "doubted_by": ["shared_body"], "max_held": 200, "max_weeks": 4},
}  # fmt: skip


# Evaluations on main, by the part of the settings a deciding change touches (decides(): a rule's id, "readiness"
# or "guard"): the folder (its README, RESULTS.md and SHA256SUMS; tests/test_flags.py checks they exist) and the
# exact value of that part that was evaluated (deciding_part). The admin's Publish refuses a draft whose settings
# change how a rule decides to a value not evaluated here, so an entry lands only with its reviewed evaluation
# (CLAUDE.md steps 2-7). The guard's evaluation is ready to label (eval/guard/README.md): no entry yet.
EVALUATED: dict[str, dict] = {}


def deciding_part(doc: dict, name: str) -> Any:
    """The part of the settings that decides, for one name of decides(): a rule's family, polarity and proposal,
    readiness, or the guard's mode and doubters."""
    s = check(doc, strict=False)
    if name == "readiness":
        return s["readiness"]
    if name == "guard":
        return {k: s["guard"][k] for k in ("mode", "doubted_by")}
    r = next(r for r in s["rules"] if r["id"] == name)
    return {k: r.get(k) for k in ("family", "polarity", "proposes")}


def evaluation_needed(old: dict, new: dict) -> list[str]:
    """The parts whose deciding change from `old` to `new` has no evaluation of the new value in EVALUATED."""
    names = sorted({c.split(".")[0] for c in decides(old, new)})
    return [n for n in names if (EVALUATED.get(n) or {}).get("settings") != deciding_part(new, n)]


def check(doc: object, strict: bool = True) -> dict:
    """Settings the deployed code can read, normalized, or SettingsError with every problem. Normalized: each rule's
    params filled from its type's defaults, and a rule type the settings don't list (one added to the code after
    they were saved) given its defaults, so adding a rule never invalidates a saved version or a served snapshot.
    A rule the code no longer has is an error in an edit (strict), and left out when reading a saved document."""
    p: list[str] = []
    if not isinstance(doc, dict):
        raise SettingsError(["settings must be an object"])
    if doc.get("schema") != SCHEMA_VERSION:
        p.append(f"schema must be {SCHEMA_VERSION}")
    extra = set(doc) - {"schema", "rules", "readiness", "guard"}
    if extra:
        p.append(f"unknown keys: {', '.join(sorted(extra))}")
    out: dict = {"schema": SCHEMA_VERSION, "rules": []}
    rules = doc.get("rules")
    if not isinstance(rules, list):
        p.append("rules must be a list")
        rules = []
    seen = set()
    for i, r in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(r, dict) or r.get("id") not in RULE_TYPES:
            if strict or not isinstance(r, dict):
                p.append(f"{where}: id must be one of {', '.join(RULE_TYPES)}")
            continue
        t = RULE_TYPES[r["id"]]
        where = r["id"]
        if t.id in seen:
            p.append(f"{where}: listed twice")
        seen.add(t.id)
        unknown = set(r) - {"id", "family", "polarity", "params", "proposes"}
        if unknown:
            p.append(f"{where}: unknown keys {', '.join(sorted(unknown))}")
        if r.get("family") not in FAMILIES:
            p.append(f"{where}: family must be one of {', '.join(FAMILIES)}")
        pol = r.get("polarity")
        dirs = list(t.polarity)
        if dirs == [""]:
            if pol not in POLARITIES:
                p.append(f"{where}: polarity is required: one of {', '.join(POLARITIES)}")
            pol = {"": pol}
        elif (
            not isinstance(pol, dict)
            or set(pol) != set(dirs)
            or not all(v in POLARITIES for v in pol.values())
        ):
            p.append(
                f"{where}: polarity is required for each of {', '.join(dirs)}: one of {', '.join(POLARITIES)}"
            )
            pol = {}
        params = r.get("params") or {}
        if not isinstance(params, dict) or set(params) - set(t.params):
            p.append(f"{where}: params may only be {', '.join(t.params) or 'none'}")
            params = {}
        norm = {}
        for k, spec in t.params.items():
            v = params.get(k, spec.default)
            if spec.kind is int and not (type(v) is int and spec.lo <= v <= spec.hi):
                p.append(f"{where}: {k} must be a whole number from {spec.lo} to {spec.hi}")
            if spec.kind is list and not (
                isinstance(v, list) and spec.lo <= len(v) <= spec.hi
                and all(isinstance(x, str) and NAME.fullmatch(x.strip().lower()) for x in v)
            ):  # fmt: skip
                p.append(
                    f"{where}: {k} must be a list of {spec.lo} to {spec.hi} names (letters, digits, spaces, & and -)"
                )
            norm[k] = [x.strip().lower() for x in v] if spec.kind is list and isinstance(v, list) else v
        proposes = r.get("proposes", t.proposes)
        if proposes is not None and proposes not in PROPOSALS:
            p.append(f"{where}: proposes must be one of {', '.join(PROPOSALS)}")
        out["rules"].append({"id": t.id, "family": r.get("family"), "polarity": pol if dirs != [""] else pol.get(""),
                             **({"params": norm} if t.params else {}),
                             **({"proposes": proposes} if proposes else {})})  # fmt: skip
    out["rules"] += [copy.deepcopy(d) for d in DEFAULTS["rules"] if d["id"] not in seen]
    order = list(RULE_TYPES)
    out["rules"].sort(key=lambda r: order.index(r["id"]))
    ready = (doc.get("readiness") or {}).get("removal") if isinstance(doc.get("readiness"), dict) else None
    if not isinstance(ready, dict):
        p.append("readiness.removal is required")
        ready = {}
    if not (type(ready.get("supports_min")) is int and 1 <= ready["supports_min"] <= len(FAMILIES)):
        p.append(f"readiness.removal.supports_min must be 1 to {len(FAMILIES)}")
    if not isinstance(ready.get("distinct_families"), bool):
        p.append("readiness.removal.distinct_families must be true or false")
    if ready.get("live_check") != "required":
        p.append("readiness.removal.live_check must be 'required'")
    blocked = ready.get("blocked_by")
    if not (isinstance(blocked, list) and set(blocked) <= {"doubts", "coverage"} and "doubts" in blocked):
        p.append("readiness.removal.blocked_by must include doubts, and may include coverage")
    out["readiness"] = {"removal": {k: ready.get(k) for k in ("supports_min", "distinct_families", "live_check",
                                                               "blocked_by")}}  # fmt: skip
    g = doc.get("guard")
    if not isinstance(g, dict):
        p.append("guard is required")
        g = {}
    if g.get("mode") not in GUARD_MODES:
        p.append(f"guard.mode must be one of {', '.join(GUARD_MODES)}")
    doubters = g.get("doubted_by")
    if not (isinstance(doubters, list) and doubters and set(doubters) <= set(RULE_TYPES)):
        p.append("guard.doubted_by must list one or more rules")
    else:
        bad = [d for d in doubters for r in out["rules"] if r["id"] == d and "doubts" not in _pols(r)]
        if bad:
            p.append(f"guard.doubted_by: {', '.join(bad)} must have polarity doubts")
    for k, hi in (("max_held", 100_000), ("max_weeks", 52)):
        if not (type(g.get(k)) is int and 0 <= g[k] <= hi):
            p.append(f"guard.{k} must be a whole number from 0 to {hi}")
    out["guard"] = {k: g.get(k) for k in ("mode", "doubted_by", "max_held", "max_weeks")}
    if p:
        raise SettingsError(p)
    return out


def _pols(r: dict) -> set[str]:
    return set(r["polarity"].values()) if isinstance(r["polarity"], dict) else {r["polarity"]}


def default() -> dict:
    return copy.deepcopy(DEFAULTS)


def decides(old: dict, new: dict) -> list[str]:
    """What an edit changes in *how* rules decide (CLAUDE.md: a change to how a rule decides needs the full
    evaluation whatever it counts): a rule's family or polarity, readiness, the guard's mode or what doubts it.
    Params and the guard's caps change which products a rule finds, not how it decides: those take the count
    (step 9's running total)."""
    a, b = check(old, strict=False), check(new, strict=False)
    out = []
    ra, rb = ({r["id"]: r for r in x["rules"]} for x in (a, b))
    for i in sorted(set(ra) | set(rb)):
        for k in ("family", "polarity", "proposes"):
            if (ra.get(i) or {}).get(k) != (rb.get(i) or {}).get(k):
                out.append(f"{i}.{k}")
    if a["readiness"] != b["readiness"]:
        out.append("readiness")
    for k in ("mode", "doubted_by"):
        if a["guard"][k] != b["guard"][k]:
            out.append(f"guard.{k}")
    return out


def changed(old: dict, new: dict) -> list[str]:
    """Every setting an edit changes, as dotted paths."""

    def flat(x: Any, at: str = "") -> dict[str, str]:
        if isinstance(x, dict):
            return {
                k: v
                for key, val in x.items()
                for k, v in flat(val, f"{at}.{key}" if at else str(key)).items()
            }
        return {at: json.dumps(x, sort_keys=True)}

    fa, fb = flat(_by_id(check(old, strict=False))), flat(_by_id(check(new, strict=False)))
    return sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k))


def _by_id(doc: dict) -> dict:
    return doc | {"rules": {r["id"]: {k: v for k, v in r.items() if k != "id"} for r in doc["rules"]}}


def dbt_var(doc: dict | None) -> dict:
    """The settings as dbt's var `rules`: {rules: {id: {family, polarity: {direction: polarity}, params}},
    readiness, guard}."""
    s = check(doc if doc is not None else DEFAULTS, strict=False)
    return {
        "rules": {r["id"]: {"family": r["family"], "params": r.get("params", {}),
                            "polarity": r["polarity"] if isinstance(r["polarity"], dict) else {"": r["polarity"]}}
                  for r in s["rules"]},
        "readiness": s["readiness"]["removal"],
        "guard": s["guard"],
    }  # fmt: skip


def schema() -> dict:
    """The settings' JSON Schema, generated from RULE_TYPES (shown read-only in the admin; check() enforces it)."""
    pol = {"enum": list(POLARITIES)}
    rules = []
    for t in RULE_TYPES.values():
        dirs = list(t.polarity)
        params = {k: ({"type": "integer", "minimum": s.lo, "maximum": s.hi} if s.kind is int else
                      {"type": "array", "items": {"type": "string"}, "minItems": s.lo, "maxItems": s.hi})
                  for k, s in t.params.items()}  # fmt: skip
        rules.append({"type": "object", "description": t.doc, "required": ["id", "family", "polarity"],
                      "properties": {"id": {"const": t.id}, "family": {"enum": list(FAMILIES)},
                                     "polarity": pol if dirs == [""] else
                                     {"type": "object", "required": dirs, "properties": dict.fromkeys(dirs, pol)},
                                     "params": {"type": "object", "properties": params,
                                                "additionalProperties": False},
                                     "proposes": {"enum": list(PROPOSALS)}}})  # fmt: skip
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["schema", "rules", "readiness", "guard"],
        "properties": {
            "schema": {"const": SCHEMA_VERSION},
            "rules": {"type": "array", "items": {"oneOf": rules}},
            "readiness": {"type": "object"},
            "guard": {"type": "object", "properties": {"mode": {"enum": list(GUARD_MODES)}}},
        },
    }
