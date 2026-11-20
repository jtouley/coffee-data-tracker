"""Draft builds, decisions snapshots and the publish job (docs/design/admin-registry.md §1-4): a draft applies the
owner's decisions from a snapshot checked against its digest, is recorded but never served, stays out of rollback's
and build-diff's defaults, and is served only by publish-release; the weekly build then applies the served build's
decisions from the lake alone. Corrections become candidate train labels, never held-out ones."""

import argparse
import json
import shutil

import duckdb
import pytest

from coffeedata import cli, decisions, quality
from coffeedata.lake import Lake

from .lakehelp import product, run

RID = "onyxcoffeelab.com"
FULL = [product(str(i)) for i in range(30)] + [product("mug", coffee=False)]


def _decision(kind, subject, action, n=1, coffee_id=None):
    return {"decision_id": f"d{n}", "subject_kind": kind, "subject": subject, "action": action,
            "reason": "test", "actor": "owner", "decided_at": "2026-10-20T00:00:00Z",
            "coffee_id": coffee_id or (subject if kind == "coffee_id" else None)}  # fmt: skip


def _write_snapshot(folder, sid, items):
    doc = decisions.snapshot(sid, "2026-10-20T00:00:00Z", items)
    path = folder / decisions.key(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, sort_keys=True))
    return decisions.digest(doc)


def test_a_snapshot_is_checked_against_its_digest():
    doc = decisions.snapshot("s1", "t", [_decision("coffee_id", "r:1", "exclude")])
    assert decisions.verified(doc, "s1", decisions.digest(doc)) is doc
    tampered = json.loads(json.dumps(doc))
    tampered["decisions"][0]["action"] = "include"
    with pytest.raises(decisions.SnapshotError, match="digest"):
        decisions.verified(tampered, "s1", decisions.digest(doc))
    with pytest.raises(decisions.SnapshotError, match="two decisions"):
        decisions.check(doc | {"decisions": doc["decisions"] * 2})
    with pytest.raises(decisions.SnapshotError):
        decisions.check(doc | {"decisions": [_decision("roaster", "x", "exclude")]})
    with pytest.raises(decisions.SnapshotError, match="COFFEEDATA_SNAPSHOTS"):
        decisions.load("s1", decisions.digest(doc), root="")
    with pytest.raises(ValueError):
        decisions.key("../etc")


def test_corrections_are_train_labels_and_never_held_out_ones(tmp_path):
    """export-labels: each include/exclude is one labeler's candidate label for the train set. A coffee in the
    sealed test set or the validation set is left out and only counted; without those files nothing is exported."""
    repo = tmp_path / "repo"
    (repo / "eval" / "sealed").mkdir(parents=True)
    (repo / "tests" / "fixtures").mkdir(parents=True)
    (repo / "eval" / "sealed" / "sample.json").write_text(json.dumps({"items": [{"coffee_id": "r:sealed"}]}))
    (repo / "tests" / "fixtures" / "classifier_labels.jsonl").write_text(
        json.dumps({"set": "validation", "coffee_id": "r:val"})
        + "\n"
        + json.dumps({"set": "train", "coffee_id": "r:tr"})
    )
    doc = decisions.snapshot("s2", "t", [
        _decision("coffee_id", "r:ok", "include", 1), _decision("coffee_id", "r:sealed", "exclude", 2),
        _decision("coffee_id", "r:val", "exclude", 3),
        _decision("input_hash", "h1", "exclude", 4, "r:tr") | {"coffee_ids": ["r:tr", "r:sealed"]},
        _decision("input_hash", "h2", "exclude", 5),
    ])  # fmt: skip
    rows, skipped = decisions.export_labels(doc, decisions.held_out(repo))
    assert [(r["coffee_id"], r["label"], r["set"], r["labeler"]) for r in rows] == [
        ("r:ok", "coffee", "train", "owner"),
        ("r:tr", "not_coffee", "train", "owner"),
    ]
    assert skipped == 4  # r:sealed, r:val, h1's r:sealed, and h2 (no coffees listed for its version)
    with pytest.raises(decisions.SnapshotError, match="held-out"):
        decisions.held_out(tmp_path / "nothing")
    # the real repository's held-out sets are found (ids only)
    assert len(decisions.held_out(__import__("pathlib").Path("."))) > 500


@pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")
def test_a_draft_applies_its_snapshot_is_never_served_and_publish_serves_it(lake, monkeypatch, tmp_path):
    real = cli.dbt_build
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: real(
        lk, db, (extra or {}) | {"min_attempted_share": 0, "min_ok_share": 0, "min_adapter_ok_share": 0}))  # fmt: skip
    monkeypatch.setattr(
        cli, "MARTS", ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event", "fct_shot"]
    )
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    monkeypatch.setattr(cli, "_today", lambda: "2026-10-19")
    snaps = tmp_path / "snapshots"
    monkeypatch.setenv("COFFEEDATA_SNAPSHOTS", str(snaps))

    def args(**kw):
        return argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date="2026-10-19",
                                  accept_volume_change=False, **kw)  # fmt: skip

    def coffees(build_id):
        f = quality.release_files(Lake(), build_id)["dim_coffee"]
        return {r[0] for r in duckdb.connect().execute(f"select coffee_id from '{f}'").fetchall()}

    run(lake, "2026-10-19", RID, FULL)
    assert cli.cmd_build(args()) is True
    (base,) = [b for b, _, _ in quality.builds(lake)]
    served_marts = (lake.root / "marts" / "dim_coffee.parquet").read_bytes()
    assert f"{RID}:3" in coffees(base) and f"{RID}:mug" not in coffees(base)

    # A draft: exclude one coffee, include the mug (by its version's hash), and a stale tab's tampered file fails.
    mug_hash = (
        duckdb.connect()
        .execute(
            f"select input_hash from '{quality.ops_files(lake, base)['product_decisions']}' where coffee_id = ?",
            [f"{RID}:mug"],
        )
        .fetchone()[0]
    )
    hash4 = (
        duckdb.connect()
        .execute(
            f"select input_hash from '{quality.ops_files(lake, base)['product_decisions']}' where coffee_id = ?",
            [f"{RID}:4"],
        )
        .fetchone()[0]
    )
    # A conflict: coffee 4's version is excluded, coffee 4 itself included. The coffee's own decision wins.
    sha = _write_snapshot(snaps, "s1", [_decision("coffee_id", f"{RID}:3", "exclude", 1),
                                        _decision("input_hash", mug_hash, "include", 2, f"{RID}:mug"),
                                        _decision("input_hash", hash4, "exclude", 3, f"{RID}:4"),
                                        _decision("coffee_id", f"{RID}:4", "include", 4)])  # fmt: skip
    with pytest.raises(SystemExit, match="digest"):
        cli.cmd_build(args(draft="d1", snapshot="s1", snapshot_sha256="0" * 64))
    assert cli.cmd_build(args(draft="d1", snapshot="s1", snapshot_sha256=sha)) is True
    draft = next(b for b, d in quality.drafts(lake).items() if d == "d1")
    assert coffees(draft) == (coffees(base) - {f"{RID}:3"}) | {f"{RID}:mug"}
    # recorded, not served: marts and the release pointer are unchanged, and the defaults skip it
    assert (lake.root / "marts" / "dim_coffee.parquet").read_bytes() == served_marts
    assert quality.served(lake)["build_id"] == base
    assert quality.unpublished_drafts(lake) == {draft}
    assert quality.build_diff(lake).get("new") != draft  # only the base is left: nothing to compare
    with pytest.raises(ValueError, match="never published"):
        quality.rollback(lake, draft)
    changes = (
        duckdb.connect()
        .execute(
            f"select change, reason, count(*) from '{quality.ops_files(lake, draft)['release_changes']}' "
            "where table_name = 'public_coffee' group by all order by all"
        )
        .fetchall()
    )
    assert ("removed", "decision:exclude", 1) in changes and ("added", "decision:include", 1) in changes
    pd = dict(duckdb.connect().execute(
        f"select coffee_id, reason from '{quality.ops_files(lake, draft)['product_decisions']}'"
    ).fetchall())  # fmt: skip
    assert pd[f"{RID}:3"] == "decision:exclude" and pd[f"{RID}:mug"] == "decision:include"

    # Publish refuses a stale baseline, then serves the draft (action publish); a retry is idempotent.
    monkeypatch.setenv("COFFEEDATA_LAKE", str(lake.root))
    pub = ["publish-release", "--build", draft, "--action", "publish", "--reason=--looks-like-a-flag"]
    with pytest.raises(SystemExit, match="baseline"):
        cli.main([*pub, "--baseline", "other"])
    cli.main([*pub, "--baseline", base])
    cli.main([*pub, "--baseline", base])
    log = (
        duckdb.connect()
        .execute(f"select action, build_id from {lake.scan('serving_log')} order by served_at")
        .fetchall()
    )
    assert [a for a, b in log if b == draft] == ["publish"]
    assert quality.unpublished_drafts(lake) == set()
    with pytest.raises(SystemExit):  # rollback is for earlier releases, publish for drafts
        cli.main(
            ["publish-release", "--build", base, "--action", "publish", "--baseline", draft, "--reason", "x"]
        )

    # The weekly build applies the served build's decisions from the lake alone: no bucket needed.
    monkeypatch.delenv("COFFEEDATA_SNAPSHOTS")
    shutil.rmtree(snaps)
    assert cli.cmd_build(args()) is True
    weekly = quality.served(lake)["build_id"]
    assert weekly not in (base, draft) and coffees(weekly) == coffees(draft)
    assert quality.build_snapshot(lake, weekly)["snapshot_id"] == "s1"

    # Roll back to the first release through the publish job's other action.
    cli.main(["publish-release", "--build", base, "--action", "rollback", "--reason", "bad publish"])
    assert quality.served(lake)["build_id"] == base


@pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")
def test_a_version_include_never_breaks_a_build_over_an_aliass_copy(lake, monkeypatch, tmp_path):
    """A version decision (input_hash) applies to every coffee with that hash, and the hash has no roaster in it, so
    an alias's copy of the store's product is decided include too, while dim_coffee leaves aliases out (#137). Neither
    a draft with such a decision nor an alias resolved after it was published fails decisions_applied: the
    canonical roaster's copy is the one that is published."""
    from datetime import UTC, datetime

    from .lakehelp import probe

    real = cli.dbt_build
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: real(
        lk, db, (extra or {}) | {"min_attempted_share": 0, "min_ok_share": 0, "min_adapter_ok_share": 0}))  # fmt: skip
    monkeypatch.setattr(
        cli, "MARTS", ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event", "fct_shot"]
    )
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    today = {"d": "2026-10-19"}
    monkeypatch.setattr(cli, "_today", lambda: today["d"])
    snaps = tmp_path / "snapshots"
    monkeypatch.setenv("COFFEEDATA_SNAPSHOTS", str(snaps))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(lake.root))

    def args(run_date, **kw):
        return argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date=run_date,
                                  accept_volume_change=True, **kw)  # fmt: skip

    def coffees(build_id):
        f = quality.release_files(Lake(), build_id)["dim_coffee"]
        return {r[0] for r in duckdb.connect().execute(f"select coffee_id from '{f}'").fetchall()}

    main, alias = RID, "onyxbeans.example"
    lake.write(
        "registry",
        "registry/source=overture/x.parquet",
        [{"added_at": datetime(2026, 10, 1, tzinfo=UTC), "source": "overture", "roaster_id": alias,
          "name": "Second Storefront"}],
    )  # fmt: skip
    probe(lake, "2026-10-18", alias)  # its own store, for now
    for rid in (main, alias):
        run(lake, "2026-10-19", rid, FULL)
    assert cli.cmd_build(args("2026-10-19")) is True
    (base,) = [b for b, _, _ in quality.builds(lake)]
    assert {f"{main}:mug", f"{alias}:mug"}.isdisjoint(coffees(base))
    mug_hash = duckdb.connect().execute(
        f"select input_hash from '{quality.ops_files(lake, base)['product_decisions']}' where coffee_id = ?",
        [f"{main}:mug"],
    ).fetchone()[0]  # fmt: skip

    # Published before the alias is known: both copies are included, as two stores' products.
    sha = _write_snapshot(snaps, "s1", [_decision("input_hash", mug_hash, "include", 1, f"{main}:mug")])
    assert cli.cmd_build(args("2026-10-19", draft="d1", snapshot="s1", snapshot_sha256=sha)) is True
    d1 = next(b for b, d in quality.drafts(lake).items() if d == "d1")
    assert {f"{main}:mug", f"{alias}:mug"} <= coffees(d1)
    pub = ["publish-release", "--build", d1, "--action", "publish", "--baseline", base]
    cli.main([*pub, "--reason", "a gift set"])

    # The alias is resolved: the weekly build pins the served decisions, and the alias's copy is left out.
    probe(lake, "2026-10-20", alias, final_host=f"www.{main}")
    for rid in (main, alias):
        run(lake, "2026-10-26", rid, FULL)
    today["d"] = "2026-10-26"
    assert cli.cmd_build(args("2026-10-26")) is True
    weekly = quality.served(lake)["build_id"]
    assert (
        weekly != d1
        and f"{main}:mug" in coffees(weekly)
        and not any(c.startswith(alias) for c in coffees(weekly))
    )

    # A new draft with the same version include, after the alias is resolved, builds too.
    sha = _write_snapshot(snaps, "s2", [_decision("input_hash", mug_hash, "include", 2, f"{main}:mug")])
    assert cli.cmd_build(args("2026-10-26", draft="d2", snapshot="s2", snapshot_sha256=sha)) is True
    d2 = next(b for b, d in quality.drafts(lake).items() if d == "d2")
    assert f"{main}:mug" in coffees(d2) and f"{alias}:mug" not in coffees(d2)
