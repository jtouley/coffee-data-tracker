"""Rules and flags (docs/design/admin-registry.md §2.3-2.4): the settings schema comes from the code's rule types;
polarity is required and a doubt is never a vote to remove; the snapshot carries the settings and the crawl flags;
flags change nothing public: a build with them is the classifier's and the owner's decisions alone."""

import argparse
import json
from pathlib import Path

import duckdb
import pytest
import yaml

from coffeedata import cli, decisions, ingest, quality, rules
from coffeedata.lake import Lake

from .lakehelp import batch, product, run

ROOT = Path(__file__).resolve().parent.parent
RID = "onyxcoffeelab.com"


def test_the_defaults_are_valid_and_are_dbts_defaults():
    assert rules.check(rules.DEFAULTS) == rules.DEFAULTS
    project = yaml.safe_load((ROOT / "dbt" / "dbt_project.yml").read_text())
    assert project["vars"]["rules"] == rules.dbt_var(None)
    assert {r["id"] for r in rules.DEFAULTS["rules"]} == set(rules.RULE_TYPES)


def test_adding_a_rule_is_one_entry_and_its_model():
    """Each rule type has its dbt model (the flag_* contract) and is documented; each evaluation named in EVALUATED
    is a folder on main with its README, RESULTS and SHA256SUMS."""
    schema = (ROOT / "dbt" / "models" / "schema.yml").read_text()
    for rid in rules.RULE_TYPES:
        sql = (ROOT / "dbt" / "models" / "intermediate" / "flags" / f"flag_{rid}.sql").read_text()
        assert "evidence_key" in sql and "direction" in sql, rid
        assert f"name: flag_{rid}" in schema, rid
    for name, e in rules.EVALUATED.items():
        folder = ROOT / e["folder"]
        assert all((folder / f).exists() for f in ("README.md", "RESULTS.md", "SHA256SUMS")), name
    assert set(rules.schema()["properties"]) == {"schema", "rules", "readiness"}


def _with(**changes):
    doc = rules.default()
    for path, value in changes.items():
        *head, last = path.split("__")
        node = doc
        for k in head:
            node = next(r for r in node if r["id"] == k) if isinstance(node, list) else node[k]
        node[last] = value
    return doc


@pytest.mark.parametrize(
    ("doc", "problem"),
    [
        (_with(rules__shared_body__polarity=None), "shared_body: polarity is required"),
        (_with(rules__shared_body__polarity="votes"), "shared_body: polarity is required"),
        (
            _with(rules__verdict_flip__polarity={"added": "supports"}),
            "verdict_flip: polarity is required for each",
        ),
        (_with(rules__shared_body__family="nope"), "family must be one of"),
        (_with(rules__shared_body__params={"min_products": 1}), "min_products must be a whole number"),
        (_with(rules__non_coffee_type__params={"types": ["drink'; drop"]}), "types must be a list"),
        (
            _with(
                readiness__removal={
                    "supports_min": 2,
                    "distinct_families": True,
                    "live_check": "optional",
                    "blocked_by": ["doubts"],
                }
            ),
            "live_check must be 'required'",
        ),  # fmt: skip
        (
            _with(
                readiness__removal={
                    "supports_min": 2,
                    "distinct_families": True,
                    "live_check": "required",
                    "blocked_by": ["coverage"],
                }
            ),
            "blocked_by must include doubts",
        ),  # fmt: skip
    ],
)
def test_settings_the_code_cant_read_are_refused_with_every_problem(doc, problem):
    with pytest.raises(rules.SettingsError) as e:
        rules.check(doc)
    assert any(problem in p for p in e.value.problems), e.value.problems


def test_a_rule_added_or_removed_in_code_never_breaks_saved_settings():
    """Settings saved before a rule type existed get its defaults; a rule the code no longer has is refused in an
    edit and left out of a saved document (a served snapshot must keep building)."""
    doc = rules.default()
    doc["rules"] = doc["rules"][:2]
    assert rules.check(doc) == rules.DEFAULTS
    gone = rules.default()
    gone["rules"].append({"id": "retired_rule", "family": "listing", "polarity": "supports"})
    with pytest.raises(rules.SettingsError, match="id must be one of"):
        rules.check(gone)
    assert rules.check(gone, strict=False) == rules.DEFAULTS
    assert decisions.settings_of(decisions.snapshot("s", "t", [], gone)) == rules.DEFAULTS
    with pytest.raises(rules.SettingsError, match="unknown keys: extra"):
        rules.check(rules.default() | {"extra": 1})


def test_what_decides_and_what_only_counts(monkeypatch):
    """CLAUDE.md: a family, polarity or proposal, or readiness, changes how a rule decides (the full evaluation,
    whatever it counts); a param changes which products a rule finds. A return to the defaults never needs one."""
    base = rules.default()
    flipped = _with(rules__verdict_flip__polarity={"added": "doubts", "removed": "restore"})
    assert rules.decides(base, flipped) == ["verdict_flip.polarity"]
    assert rules.changed(base, flipped) == ["rules.verdict_flip.polarity.added"]
    params = _with(rules__shared_body__params={"min_products": 4, "min_chars": 60})
    assert rules.decides(base, params) == [] and rules.changed(base, params) == [
        "rules.shared_body.params.min_products"
    ]
    assert rules.evaluation_needed(base, flipped) == ["verdict_flip"]
    assert rules.evaluation_needed(flipped, base) == []  # back to the defaults: always allowed
    monkeypatch.setitem(rules.EVALUATED, "verdict_flip", {"folder": "eval/x",
                                                          "settings": rules.deciding_part(flipped, "verdict_flip")})  # fmt: skip
    assert rules.evaluation_needed(base, flipped) == []  # the evaluated value


def test_the_snapshot_carries_settings_and_crawl_flags():
    on = _with(rules__shared_body__params={"min_products": 5, "min_chars": 60})
    doc = decisions.snapshot("s1", "t", [], on, [{"roaster_id": "b.example", "crawl": False},
                                                 {"roaster_id": "a.example", "crawl": True}])  # fmt: skip
    assert decisions.settings_of(doc) == rules.check(on)
    assert decisions.crawl_off(doc) == {"b.example"}
    assert decisions.settings_of(None) == rules.DEFAULTS and decisions.crawl_off(None) == set()
    # A snapshot without them is the one #128 wrote: same digest, the defaults
    plain = decisions.snapshot("s2", "t", [])
    assert set(plain) == {"version", "snapshot_id", "created_at", "decisions"}
    with pytest.raises(decisions.SnapshotError, match="rule settings"):
        decisions.check(doc | {"settings": _with(rules__shared_body__polarity="votes")})
    with pytest.raises(decisions.SnapshotError, match="roasters"):
        decisions.check(doc | {"roasters": [{"roaster_id": "x"}]})


def _serve_snapshot(lake: Lake, build: str, doc: dict) -> None:
    lake.write("builds", f"builds/run_date=2026-10-19/build={build}.parquet",
               [{"build_id": build, "kind": "snapshot", "unique_id": doc["snapshot_id"],
                 "checksum": decisions.digest(doc), "payload": json.dumps(doc, sort_keys=True)}])  # fmt: skip
    (lake.root / "marts").mkdir(exist_ok=True)
    duckdb.connect().execute(
        f"copy (select '{build}' as build_id, date '2026-10-19' as run_date) to '{lake.root}/marts/release.parquet'"
    )


def test_scheduled_crawls_skip_a_crawl_off_roaster_and_a_live_check_doesnt(lake, monkeypatch):
    from coffeedata import registry
    from coffeedata.models import Roaster

    states = [registry.RoasterState(Roaster(roaster_id=r, name=r, host=r, platform="shopify"), "active", None)
              for r in ("a.example", "b.example")]  # fmt: skip
    monkeypatch.setattr(registry, "current", lambda _lake, **k: states)
    assert ingest.crawl_off(lake) == set()  # nothing served: nothing off
    _serve_snapshot(
        lake, "b1", decisions.snapshot("s1", "t", [], None, [{"roaster_id": "b.example", "crawl": False}])
    )
    assert ingest.crawl_off(lake) == {"b.example"}
    ids = lambda x: [s.roaster.roaster_id for s in x[0]]  # noqa: E731
    assert ids(ingest.select_states(lake, "2026-10-26")) == ["a.example"]
    assert ids(ingest.select_states(lake, "2026-10-26", only="b.example")) == []
    assert ids(ingest.select_states(lake, "2026-10-26", only="b.example", test=True)) == ["b.example"]
    # crawl-retry's parked stores: a crawl-off store stays parked, except for a test crawl
    monkeypatch.setattr(ingest, "load_parked", lambda _lake, _run: {"a.example": {}, "b.example": {}})
    monkeypatch.setattr(ingest, "_done_and_tried", lambda _lake, _run: (set(), set()))
    assert ids(ingest.select_parked(lake, "2026-10-26")) == ["a.example"]
    assert ids(ingest.select_parked(lake, "2026-10-26", test=True)) == ["a.example", "b.example"]
    monkeypatch.setattr(quality, "served", lambda _lake: (_ for _ in ()).throw(OSError("429")))
    assert ingest.crawl_off(lake) == set()  # a failed read crawls everyone, as before the flag


# --- flags, through dbt ---------------------------------------------------------------------------------------

SHARED = ("This sample pack includes three quarter-pound bags of our favourite single origins, roasted to order "
          "each week.")  # fmt: skip


def _bodies(lake: Lake, run_date: str, texts: dict[str, str]) -> None:
    from datetime import date

    lake.write_partition("bodies", run_date, batch(), [
        {"body_hash": h, "body_text": t, "first_seen_run": date.fromisoformat(run_date)} for h, t in texts.items()
    ])  # fmt: skip


@pytest.mark.skipif(__import__("shutil").which("dbt") is None, reason="dbt not installed")
def test_flags_are_raised_in_the_build_and_change_nothing_public(lake, monkeypatch, tmp_path):
    """Week 1 is served with 30 coffees; three share a pasted paragraph (shared_body doubts them). In week 2 two
    coffees stop reading as coffee: 0 (doubted) and 5 (not). Both leave the catalog, as the classifier says; the
    flags record it: verdict_flip removed (restore) on both, shared_body on the three, nothing ready."""
    real = cli.dbt_build
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: real(
        lk, db, (extra or {}) | {"min_attempted_share": 0, "min_ok_share": 0, "min_adapter_ok_share": 0}))  # fmt: skip
    monkeypatch.setattr(
        cli, "MARTS", ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event", "fct_shot"]
    )
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    week1 = [product(str(i)) | ({"body_hash": "shared"} if i in (0, 1, 2) else {}) for i in range(30)]
    week2 = [p | ({"title": f"Mug {p['product_id']}", "is_coffee": False} if p["product_id"] in ("0", "5") else {})
             for p in week1]  # fmt: skip

    def args(day):
        monkeypatch.setattr(cli, "_today", lambda: day)
        return argparse.Namespace(
            db=str(tmp_path / "coffee.duckdb"), run_date=day, accept_volume_change=False
        )

    def coffees(build_id):
        f = quality.release_files(Lake(), build_id)["dim_coffee"]
        return {r[0] for r in duckdb.connect().execute(f"select coffee_id from '{f}'").fetchall()}

    _bodies(lake, "2026-10-19", {"shared": SHARED} | {f"b{i}": f"Coffee number {i}." for i in range(3, 30)})
    run(lake, "2026-10-19", RID, week1)
    assert cli.cmd_build(args("2026-10-19")) is True
    base = quality.served(lake)["build_id"]
    assert {f"{RID}:0", f"{RID}:5"} <= coffees(base)
    run(lake, "2026-10-26", RID, week2)
    assert cli.cmd_build(args("2026-10-26")) is True
    week = quality.served(lake)["build_id"]
    assert not ({f"{RID}:0", f"{RID}:5"} & coffees(week))
    f = quality.ops_files(lake, week)["mart_flags"]
    flags = (
        duckdb.connect()
        .execute(
            f"select rule, direction, polarity, coffee_id, ready_flags, public_rows from '{f}' order by all"
        )
        .fetchall()
    )
    assert ("verdict_flip", "removed", "restore", f"{RID}:0", False, 2) in flags
    assert ("verdict_flip", "removed", "restore", f"{RID}:5", False, 2) in flags
    assert {c for r, _, _, c, _, _ in flags if r == "shared_body"} == {f"{RID}:0", f"{RID}:1", f"{RID}:2"}


def test_review_fixes_retired_rules_and_wrong_shapes(monkeypatch):
    """A saved document that names a retired rule, or the cut guard, still reads; an edit of it is refused; a wrong
    shape is a SettingsError (a 400), never a TypeError (a 500)."""
    old = rules.default() | {"guard": {"mode": "off"}}
    assert decisions.settings_of({"settings": old}) == rules.DEFAULTS
    with pytest.raises(rules.SettingsError, match="unknown keys: guard"):
        rules.check(old)
    for bad in ({"schema": 1, "rules": [{"id": ["x"]}]}, _with(readiness__removal={"blocked_by": [{}]})):
        with pytest.raises(rules.SettingsError):
            rules.check(bad)
