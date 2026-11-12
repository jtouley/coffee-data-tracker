"""The owner's include and exclude decisions, as the build reads them (docs/design/admin-registry.md §2-3).

The admin keeps every decision as an event in its database. It never hands the build that database: it exports an
immutable **snapshot** of the effective decisions (one per subject, the latest event wins, a revert removes it) to
its snapshot bucket, and starts a draft build with the snapshot's id and digest. The build reads the file, checks
the digest (fail closed: a snapshot that is missing, changed or malformed stops the build), applies it (dbt's
stg_decisions, through var decisions_path), and records it in its own builds rows (kind snapshot), so the weekly
build applies the served build's decisions from the lake alone, with no bucket and no database.

A snapshot also carries the rule settings (coffeedata.rules) and the roasters whose crawl flag is off, when the
admin set them: the build reads its rules from it (no `settings`: rules.DEFAULTS), and a scheduled crawl skips
the served snapshot's crawl-off roasters (ingest.select_states). Its `prefilter` is the owner's keyword filters
(classify.KeywordFilter): the build extends classify.excluded with them (dbt stg_prefilter, through var
prefilter_path), and once the snapshot is published, classify and enrich skip what they exclude
(served_prefilter).

The same snapshot is the source of `coffeedata decide export-labels`: owner corrections as candidate labels for the
classifier's train set only (CLAUDE.md: one labeler, never a sealed test example).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path

from . import classify, rules

log = logging.getLogger(__name__)

VERSION = 1
# A snapshot carrying chain flags (`chains`) is version 2, so a pipeline image from before chain flags refuses it
# ("not a decisions snapshot (version 1)") and the draft build fails, rather than building with the seed and recording
# the snapshot, chains included, as applied. Deploy the pipeline image first, then the admin (docs/admin/README.md).
CHAINS_VERSION = 2
SUBJECTS = ("coffee_id", "input_hash")
ACTIONS = ("include", "exclude")
SNAPSHOT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
# A decision's fields in a snapshot; only subject_kind, subject and action reach dbt.
# coffee_ids: for a decision by version, every coffee sharing it (the admin's export resolves them; export-labels
# checks each against the held-out sets). evidence_hash: the store and the flags' evidence the decision was made on
# (group approval).
FIELDS = ("decision_id", "subject_kind", "subject", "action", "reason", "actor", "decided_at", "coffee_id",
          "input_hash", "roaster_id", "title", "coffee_ids", "evidence_hash")  # fmt: skip


class SnapshotError(RuntimeError):
    """A snapshot the build can't trust: missing, changed since it was made, or malformed. The build fails."""


def digest(doc: dict) -> str:
    """The snapshot's digest: SHA-256 over its canonical JSON (sorted keys), as the admin's stores write it."""
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def check(doc: object) -> list[dict]:
    """The decisions of a well-formed snapshot, or SnapshotError. At most one decision per subject; settings and
    roasters, when present, must be readable by this code."""
    if (
        not isinstance(doc, dict)
        or doc.get("version") not in (VERSION, CHAINS_VERSION)
        or not isinstance(doc.get("decisions"), list)
    ):
        raise SnapshotError("not a decisions snapshot (version 1 or 2)")
    if ("chains" in doc) != (doc["version"] == CHAINS_VERSION):
        raise SnapshotError("a snapshot carries chains exactly when it is version 2")
    if "settings" in doc:
        try:
            rules.check(doc["settings"], strict=False)
        except rules.SettingsError as e:
            raise SnapshotError(f"its rule settings can't be read: {e}") from e
    roasters_ = doc.get("roasters", [])
    if not isinstance(roasters_, list) or not all(
        isinstance(r, dict) and isinstance(r.get("roaster_id"), str) and isinstance(r.get("crawl"), bool)
        for r in roasters_
    ):
        raise SnapshotError("roasters must be a list of {roaster_id, crawl}")
    try:
        prefilter(doc)
    except (ValueError, TypeError, KeyError) as e:
        raise SnapshotError(f"its keyword filters can't be read: {e}") from e
    chains_ = doc.get("chains", [])
    if (
        not isinstance(chains_, list)
        or not all(
            isinstance(c, dict)
            and set(c) == {"roaster_id", "chain"}
            and isinstance(c["roaster_id"], str)
            and c["roaster_id"]
            and isinstance(c["chain"], bool)
            for c in chains_
        )
        or len({c["roaster_id"] for c in chains_}) != len(chains_)
    ):
        raise SnapshotError("chains must be a list of {roaster_id, chain}, one per roaster")
    for k in ("sources", "not_roasters"):
        listed = doc.get(k, [])
        if not isinstance(listed, list) or not all(isinstance(r, str) and r for r in listed):
            raise SnapshotError(f"{k} must be a list of domains")
    seen: set[tuple[str, str]] = set()
    out = []
    for d in doc["decisions"]:
        if not isinstance(d, dict):
            raise SnapshotError("a decision is not an object")
        kind, subject, action = d.get("subject_kind"), d.get("subject"), d.get("action")
        if kind not in SUBJECTS or action not in ACTIONS or not isinstance(subject, str) or not subject:
            raise SnapshotError(f"bad decision {d.get('decision_id')!r}")
        if (kind, subject) in seen:
            raise SnapshotError(f"two decisions for {kind} {subject}")
        seen.add((kind, subject))
        out.append({f: d.get(f) for f in FIELDS})
    return out


def snapshot(snapshot_id: str, created_at: str, decisions: Iterable[dict], settings: dict | None = None,
             roasters: Iterable[dict] = (), sources: Iterable[str] = (),
             not_roasters: Iterable[str] = (), keyword_filters: Iterable[dict] = (),
             chains: Iterable[dict] | None = None) -> dict:  # fmt: skip
    """A snapshot document. `decisions` are effective decisions (one per subject); `settings` the rule settings
    version to build with (left out: the defaults); `roasters` each roaster's current crawl flag where it was set;
    `sources` the source-list places the owner flagged as roasters (the registry adds them: registry.current);
    `not_roasters` the registry's roasters the owner flagged not a roaster (the registry drops them);
    `keyword_filters` the owner's keyword filters ({phrase, field}; left out when there are none); `chains` the
    owner's chain flags ({roaster_id, chain}: over the store count, either way), kept even when empty, since an
    empty list means "no override", not "the seed's" (None: left out, as before chain flags existed)."""
    if not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("bad snapshot id")
    doc = {"version": VERSION, "snapshot_id": snapshot_id, "created_at": created_at,
           "decisions": sorted(({f: d.get(f) for f in FIELDS} for d in decisions),
                               key=lambda d: (d["subject_kind"], d["subject"]))}  # fmt: skip
    if settings is not None:
        doc["settings"] = settings
    if roasters := sorted(({"roaster_id": r["roaster_id"], "crawl": bool(r["crawl"])} for r in roasters),
                          key=lambda r: r["roaster_id"]):  # fmt: skip
        doc["roasters"] = roasters
    if sources := sorted(set(sources)):
        doc["sources"] = sources
    if not_roasters := sorted(set(not_roasters)):
        doc["not_roasters"] = not_roasters
    if kf := classify.keyword_filters(keyword_filters):
        doc["prefilter"] = [f.as_dict() for f in kf]
    if chains is not None:
        # as given (check() refuses a chain that isn't a JSON boolean: "false" is no False)
        doc["version"] = CHAINS_VERSION
        doc["chains"] = sorted(({"roaster_id": c.get("roaster_id"), "chain": c.get("chain")} for c in chains),
                               key=lambda c: str(c["roaster_id"]))  # fmt: skip
    check(doc)
    return doc


def chain_flags(doc: dict | None) -> list[dict] | None:
    """The chain flags a snapshot builds with ([{roaster_id, chain}], dbt var chain_flags), or None when it carries
    none (a snapshot from before chain flags, or no snapshot): dbt then reads its seed, dbt/seeds/chain_overrides.csv."""
    if not doc or "chains" not in doc:
        return None
    return [dict(c) for c in doc["chains"]]


def prefilter(doc: dict | None) -> tuple[classify.KeywordFilter, ...]:
    """The keyword filters a snapshot builds with (none: no `prefilter`). ValueError when one can't be read."""
    rows = (doc or {}).get("prefilter", [])
    if not isinstance(rows, list) or not all(
        isinstance(r, dict) and set(r) == {"phrase", "field"} for r in rows
    ):
        raise ValueError("prefilter must be a list of {phrase, field}")
    return classify.keyword_filters(rows)


def served_prefilter(lake) -> tuple[classify.KeywordFilter, ...]:
    """The published snapshot's keyword filters, for the jobs that ask Claude (classify, enrich): a draft's
    filters reach them only once it is published. Fails open, with a warning: a snapshot that can't be read gives
    no filters, so every product goes to Claude as before (a cost, never a wrong exclusion)."""
    try:
        return prefilter(served_doc(lake))
    except Exception as e:  # noqa: BLE001 - a lake read or a malformed snapshot: Claude classifies as before
        log.warning("the served snapshot's keyword filters weren't read (%s): none apply", type(e).__name__)
        return ()


def write_prefilter(doc: dict | None, baseline: dict | None, path: Path) -> Path:
    """The keyword filters dbt reads (stg_prefilter): one row per filter of this build's snapshot (`applies`) or of
    the served build's (`baseline`), so the build can tell what a change newly excludes (eval_prefilter)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    now, was = set(prefilter(doc)), set(prefilter(baseline))
    rows = [f.as_dict() | {"applies": f in now, "baseline": f in was} for f in sorted(now | was)]
    schema = pa.schema([("phrase", pa.string()), ("field", pa.string()), ("applies", pa.bool_()),
                        ("baseline", pa.bool_())])  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return path


def settings_of(doc: dict | None) -> dict:
    """The rule settings a snapshot builds with: its own, else the defaults."""
    return rules.check((doc or {}).get("settings") or rules.DEFAULTS, strict=False)


def source_roasters(doc: dict | None) -> list[str]:
    """The source-list places a snapshot flags as roasters (sorted)."""
    return list((doc or {}).get("sources", []))


def not_roasters(doc: dict | None) -> list[str]:
    """The registry's roasters a snapshot flags not a roaster (sorted): never probed or crawled."""
    return list((doc or {}).get("not_roasters", []))


def served_doc(lake) -> dict | None:
    """The published snapshot: the served build's, read from its builds row (no bucket, no database); None when
    nothing is served or it has none. Raises on a failed read: each caller chooses how to fail."""
    from . import quality

    build = (quality.served(lake) or {}).get("build_id")
    pinned = quality.build_snapshot(lake, build) if build else None
    return (pinned or {}).get("doc")


def crawl_off(doc: dict | None) -> set[str]:
    """The roasters a snapshot turns the crawl flag off for."""
    return {r["roaster_id"] for r in (doc or {}).get("roasters", []) if not r["crawl"]}


def key(snapshot_id: str) -> str:
    """Where a snapshot lives in the admin's snapshot store (and so under the build's mount of it)."""
    if not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("bad snapshot id")
    return f"snapshots/{snapshot_id}.json"


def load(snapshot_id: str, sha256: str, root: str | Path | None = None) -> dict:
    """A snapshot from the snapshot store's folder (COFFEEDATA_SNAPSHOTS: the build job's read-only mount of the
    admin's snapshot bucket), checked against the digest the admin recorded when it made it."""
    if not SHA256.fullmatch(sha256 or ""):
        raise SnapshotError("a snapshot needs its sha256 digest")
    folder = root or os.environ.get("COFFEEDATA_SNAPSHOTS")
    if not folder:
        raise SnapshotError("COFFEEDATA_SNAPSHOTS isn't set: the snapshot can't be read")
    try:
        doc = json.loads((Path(folder) / key(snapshot_id)).read_text())
    except (OSError, ValueError) as e:
        raise SnapshotError(f"snapshot {snapshot_id} can't be read: {type(e).__name__}") from e
    return verified(doc, snapshot_id, sha256)


def verified(doc: object, snapshot_id: str, sha256: str) -> dict:
    """`doc`, if it is snapshot `snapshot_id` with this digest; else SnapshotError."""
    if not isinstance(doc, dict) or doc.get("snapshot_id") != snapshot_id:
        raise SnapshotError(f"snapshot {snapshot_id}: not that snapshot")
    if digest(doc) != sha256:
        raise SnapshotError(f"snapshot {snapshot_id}: its digest doesn't match (changed since it was made)")
    check(doc)
    return doc


def write_parquet(doc: dict, path: Path) -> Path:
    """The decisions dbt reads (stg_decisions), from a checked snapshot."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [{k: d[k] for k in ("decision_id", "subject_kind", "subject", "action")} for d in check(doc)]
    schema = pa.schema([(k, pa.string()) for k in ("decision_id", "subject_kind", "subject", "action")])
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return path


# --- corrections as candidate labels (design §4) -------------------------------------------------------------------

LABEL = {"include": "coffee", "exclude": "not_coffee"}


def held_out(repo: Path) -> set[str]:
    """coffee_ids that must never be exported as train labels: the sealed test set's (eval/sealed/sample.json, read
    for its ids only) and the classifier's validation set (tests/fixtures/classifier_labels.jsonl).
    Fails closed: without both files (a copy of the package with no eval/), nothing can be exported."""
    sealed = repo / "eval" / "sealed" / "sample.json"
    labels = repo / "tests" / "fixtures" / "classifier_labels.jsonl"
    if not (sealed.exists() and labels.exists()):
        raise SnapshotError(f"the held-out sets aren't in {repo}: run this from a checkout of the repository")
    out = {str(i["coffee_id"]) for i in json.loads(sealed.read_text())["items"]}
    for line in labels.read_text().splitlines():
        r = json.loads(line)
        if r.get("set") != "train":
            out.add(str(r["coffee_id"]))
    return out


def export_labels(doc: dict, held: set[str]) -> tuple[list[dict], int]:
    """The snapshot's decisions as candidate reference labels, train set only, and how many were left out because
    their coffee is held out (sealed test or validation; counted, never listed). A decision by version labels each
    coffee in its `coffee_ids`, each checked; without them it is left out. An owner correction is one labeler
    (CLAUDE.md step 6): it needs a second independent label against eval/sealed/GUIDE.md, and a third to settle a
    disagreement, before it can be part of any test set. A decision with no coffee_id can't be checked against the
    held-out sets, so it is left out too."""
    out, skipped = [], 0
    for d in check(doc):
        # A decision by version labels every coffee sharing it, each checked; one with no list of them can't be.
        ids = (d.get("coffee_ids") or []) if d["subject_kind"] == "input_hash" else [d.get("coffee_id")]
        if not ids or not all(ids):
            skipped += 1
            continue
        for cid in ids:
            if cid in held:
                skipped += 1
                continue
            out.append(_label(d, str(cid)))
    return out, skipped


def _label(d: dict, coffee_id: str) -> dict:
    return {
        "set": "train", "coffee_id": coffee_id, "input_hash": d.get("input_hash"), "label": LABEL[d["action"]],
        "reason": d.get("reason"), "labeler": "owner", "actor": d.get("actor"), "decided_at": d.get("decided_at"),
        "decision_id": d.get("decision_id"), "subject_kind": d["subject_kind"], "source": "admin override",
    }  # fmt: skip
