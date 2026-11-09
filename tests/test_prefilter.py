"""The owner's keyword filters (docs/design/admin-registry.md §4, "a keyword pre-filter"): a word or phrase matched as
whole words against a product's title, product type or tags. A match extends classify.excluded: not coffee, and no
Claude verdict. A filter only ever excludes, and when it is unsure (part of a longer word) it doesn't match. Filters
reach the build only through its snapshot; nothing in the pipeline reads the admin's database."""

import ast
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from coffeedata import classify, decisions, products
from coffeedata.classify import KeywordFilter

ROOT = Path(__file__).resolve().parents[1]


def item(title="", ptype="", tags=()):
    return products.item(title, ptype, list(tags), [340], "b", "")


@pytest.mark.parametrize(
    ("phrase", "field", "listing", "hit"),
    [
        ("apron", "title", item("Barista Apron"), True),
        ("apron", "title", item("APRON, linen"), True),  # any case, any punctuation after
        ("tea apron", "title", item("Kitchen Tea   Apron"), True),  # any run of spaces between the words
        ("apron", "title", item("Aproning Blend"), False),  # part of a longer word: unsure, no match
        ("apron", "title", item("Kitchen tea-apron"), False),  # a hyphenated word: unsure
        ("tea", "title", item("Tea's Origin Blend"), False),  # an apostrophe: unsure
        ("tea", "title", item("Teak Roast"), False),
        ("apron", "product_type", item("Guji", "Aprons & Linen"), False),  # plural is another word
        ("aprons", "product_type", item("Guji", "Aprons & Linen"), True),
        ("linen", "tags", item("Guji", "", ["Gift", "linen goods"]), True),  # within one tag
        ("gift linen", "tags", item("Guji", "", ["Gift", "linen goods"]), False),  # never across two tags
        ("apron", "title", item("Guji", "Apron"), False),  # only the field it names
        ("café", "title", item("CAFÉ Mug"), True),  # Unicode case folding
        ("brew", "title", item("Cold‐brew Blend"), False),  # U+2010 hyphen: part of a word, unsure
        ("brew", "title", item("Cold‑brew Blend"), False),  # U+2011 non-breaking hyphen
        ("brew", "title", item("Cold–brew"), False),  # en dash
        ("brew", "title", item("Brew’d Awakening"), False),  # curly apostrophe
        ("brew", "title", item("Brewʼd"), False),  # modifier letter apostrophe
        ("strasse", "title", item("Straße Espresso"), True),  # "ß" folds to "ss"
        ("straße", "title", item("STRASSE Espresso"), True),
        ("apron", "title", item("ＡＰＲＯＮ Classic"), True),  # full-width letters
        ("apron", "title", item("ＡＰＲＯＮＳ"), False),  # full-width, part of a longer word
    ],
)
def test_a_filter_matches_whole_words_in_its_field(phrase, field, listing, hit):
    assert KeywordFilter(phrase, field).matches(listing) is hit


@pytest.mark.parametrize("raw", ["", "ab", "x" * 61, "12 34", "tow(el", "a|b", ".*", None, 7])
def test_a_filter_is_a_plain_word_or_phrase(raw):
    with pytest.raises(ValueError):
        KeywordFilter(raw, "title")  # type: ignore[arg-type]


def test_a_filter_is_normalized_and_its_field_checked():
    assert KeywordFilter("  Tea   Apron ", "title").phrase == "tea apron"
    assert KeywordFilter("Kopi Luwak", "title") == KeywordFilter("kopi luwak", "title")
    with pytest.raises(ValueError):
        KeywordFilter("apron", "description")
    assert classify.keyword_filters([{"phrase": "B b", "field": "tags"}, {"phrase": "a a", "field": "title"},
                                     {"phrase": "b B", "field": "tags"}]) == (
        KeywordFilter("a a", "title"), KeywordFilter("b b", "tags"))  # fmt: skip


def test_filters_extend_excluded_and_never_rename_a_rule_of_the_code():
    apron = (KeywordFilter("apron", "title"),)
    assert not classify.excluded(item("Barista Apron Blend"))
    assert classify.excluded(item("Barista Apron Blend"), apron)
    assert classify.exclusion(item("Barista Apron Blend"), apron) == "keyword:title:apron"
    assert (
        classify.exclusion(item("Apron Mug"), apron) == "not_coffee_words"
    )  # the code's rule keeps its name
    assert classify.decision(item("Barista Apron Blend"), filters=apron) == (
        False,
        "excluded:keyword:title:apron",
    )
    assert not classify.is_coffee(item("Ethiopia Apron Blend Roast"), apron)
    assert classify.is_coffee(item("Ethiopia Apron Blend Roast"))


def test_a_filter_only_excludes_and_overrides_claudes_coffee_verdict():
    """A match is not coffee whatever Claude said; no match leaves everything as it was, verdict included."""
    apron = (KeywordFilter("apron", "title"),)
    assert products.decide_why(item("Barista Apron"), "coffee", apron) == (
        False,
        "rules",
        "excluded:keyword:title:apron",
    )
    assert products.decide_why(item("Guji"), "coffee", apron) == (True, "claude", "verdict:coffee")
    assert products.decide_why(item("Guji"), "not_coffee", apron) == (False, "claude", "verdict:not_coffee")
    assert products.rules(item("Ethiopia Apron Roast"), "coffee", apron) is None


def test_verdict_pending_skips_what_the_filters_exclude(monkeypatch):
    from coffeedata import verdict

    rows = [{"input_hash": "a", "title": "Barista Apron", "product_type": "", "tags": [], "grams": [340],
             "body_hash": "x", "body_text": ""},
            {"input_hash": "b", "title": "Guji", "product_type": "", "tags": [], "grams": [340], "body_hash": "y",
             "body_text": ""}]  # fmt: skip

    class Cur:
        description = [(k,) for k in rows[0]]

        def fetchall(self):
            return [tuple(r.values()) for r in rows]

    class Lake:
        def scan(self, t):
            return t

        def connect(self):
            return type("C", (), {"execute": lambda *a: Cur()})()

    assert [r["input_hash"] for r in verdict.pending(Lake(), "m", 10, ())] == ["a", "b"]
    assert [
        r["input_hash"] for r in verdict.pending(Lake(), "m", 10, (KeywordFilter("apron", "title"),))
    ] == ["b"]
    # without filters given, the published snapshot's: one that can't be read gives none (fail open, to Claude)
    monkeypatch.setattr(decisions, "served_doc", lambda lake: (_ for _ in ()).throw(OSError("down")))
    assert [r["input_hash"] for r in verdict.pending(Lake(), "m", 10)] == ["a", "b"]
    monkeypatch.setattr(
        decisions, "served_doc", lambda lake: {"prefilter": [{"phrase": "apron", "field": "title"}]}
    )
    assert [r["input_hash"] for r in verdict.pending(Lake(), "m", 10)] == ["b"]


# --- the snapshot ----------------------------------------------------------------------------------------------


def test_a_snapshot_carries_the_filters_sorted_and_checked():
    doc = decisions.snapshot("s1", "2026-11-09T00:00:00Z", [],
                             keyword_filters=[{"phrase": "Apron", "field": "title"}, {"phrase": "linen", "field": "tags"}])  # fmt: skip
    assert doc["prefilter"] == [{"phrase": "apron", "field": "title"}, {"phrase": "linen", "field": "tags"}]
    assert decisions.prefilter(doc) == (KeywordFilter("apron", "title"), KeywordFilter("linen", "tags"))
    assert "prefilter" not in decisions.snapshot("s2", "2026-11-09T00:00:00Z", [])  # none: left out
    assert decisions.prefilter(None) == ()
    sha = decisions.digest(doc)
    assert decisions.verified(doc, "s1", sha) == doc
    for bad in ([{"phrase": "x(", "field": "title"}], [{"phrase": "apron", "field": "body"}], "apron",
                [{"phrase": "apron", "field": "title", "regex": True}]):  # fmt: skip
        with pytest.raises(decisions.SnapshotError, match="keyword filters"):
            decisions.check(doc | {"prefilter": bad})


def test_the_build_gets_this_snapshots_filters_and_the_served_ones(tmp_path):
    now = {"prefilter": [{"phrase": "apron", "field": "title"}, {"phrase": "linen", "field": "tags"}]}
    was = {"prefilter": [{"phrase": "linen", "field": "tags"}, {"phrase": "old", "field": "title"}]}
    rows = pq.read_table(decisions.write_prefilter(now, was, tmp_path / "p.parquet")).to_pylist()
    assert rows == [
        {"phrase": "apron", "field": "title", "applies": True, "baseline": False},
        {"phrase": "linen", "field": "tags", "applies": True, "baseline": True},
        {"phrase": "old", "field": "title", "applies": False, "baseline": True},
    ]
    assert pq.read_table(decisions.write_prefilter(None, None, tmp_path / "e.parquet")).num_rows == 0


def test_stg_products_names_the_filter_and_whether_it_is_new():
    import importlib.util

    spec = importlib.util.spec_from_file_location("stg_products", ROOT / "dbt/models/staging/stg_products.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    r = {"input_hash": "h", "title": "Barista Apron Blend", "product_type": "", "tags": [], "grams": [340],
         "body_hash": "b", "body_text": ""}  # fmt: skip
    apron, linen = KeywordFilter("apron", "title"), KeywordFilter("linen", "title")
    new = m.row(r, {"h": ("coffee", "m")}, (apron,), ())
    assert (new["is_coffee"], new["classified_by"], new["prefilter"], new["prefilter_new"]) == (
        False, "rules", "keyword:title:apron", True)  # fmt: skip
    assert (
        m.row(r, {"h": ("coffee", "m")}, (apron,), (apron,))["prefilter_new"] is False
    )  # served already excluded
    assert m.row(r, {"h": ("coffee", "m")}, (apron,), (linen,))["prefilter_new"] is True
    kept = m.row(r, {"h": ("coffee", "m")}, (linen,), ())
    assert (kept["is_coffee"], kept["prefilter"], kept["prefilter_new"]) == (True, None, False)
    assert (new["unfiltered_is_coffee"], new["prefilter_restored"]) == (True, False)
    # the served build's filter is gone from this build's set: the version goes back to Claude and the rules
    back = m.row(r, {"h": ("coffee", "m")}, (), (apron,))
    assert (back["is_coffee"], back["prefilter_restored"], back["baseline_prefilter"]) == (True, True,
                                                                                         "keyword:title:apron")  # fmt: skip


# --- builds and jobs never read the admin's database -----------------------------------------------------------


def test_the_pipeline_never_imports_the_admin_or_a_database_driver():
    """Builds and jobs read the filters from the snapshot (the lake), never from the control plane: nothing under
    src/coffeedata or dbt/ imports the admin, its control plane or a Postgres driver."""
    banned = ("admin", "pg8000", "psycopg", "psycopg2", "google.cloud.sql")
    found = []
    for path in [*(ROOT / "src/coffeedata").rglob("*.py"), *(ROOT / "dbt").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else [])  # fmt: skip
            found += [
                f"{path.relative_to(ROOT)}: {n}" for n in names if n.split(".")[0] in banned or n in banned
            ]
    assert not found, found
    assert "ADMIN_DATABASE_URL" not in json.dumps(
        [p.read_text() for p in (ROOT / "src/coffeedata").rglob("*.py")]
    )


# --- the weekly check of the published filters (verdict.SHADOW_SAMPLE) ---------------------------------------------


def test_the_weekly_sample_is_deterministic_spread_across_roasters_and_capped():
    from coffeedata import verdict

    rows = [{"input_hash": f"{r}-{i}", "roaster_id": r, "prefilter": "keyword:title:apron"}
            for r in ("a.example", "b.example", "c.example") for i in range(30)]  # fmt: skip
    one = verdict.shadow_sample(rows, "2026-11-09")
    assert one == verdict.shadow_sample(
        list(reversed(rows)), "2026-11-09"
    )  # the order it's read in doesn't matter
    assert len(one) == verdict.SHADOW_SAMPLE == 20
    per = {r: sum(x["roaster_id"] == r for x in one) for r in ("a.example", "b.example", "c.example")}
    assert max(per.values()) - min(per.values()) <= 1  # one roaster at a time, in turn
    assert one != verdict.shadow_sample(rows, "2026-11-16")  # each week draws afresh
    assert (
        verdict.shadow_sample(rows[:3], "2026-11-09")
        and len(verdict.shadow_sample(rows[:3], "2026-11-09")) == 3
    )


def test_filtered_finds_what_the_filters_newly_exclude_and_nothing_the_code_does(monkeypatch):
    from coffeedata import verdict

    def r(h, title):
        return {"input_hash": h, "title": title, "product_type": "", "tags": [], "grams": [340], "body_hash": "x",
                "body_text": "", "roaster_id": "r.example"}  # fmt: skip

    rows = [r("a", "Barista Apron"), r("b", "Apron Mug"), r("c", "Guji"), r("d", "Linen Apron")]
    monkeypatch.setattr(verdict, "_pending_rows", lambda lake, model_id: iter(rows))
    apron, linen = KeywordFilter("apron", "title"), KeywordFilter("linen apron", "title")
    got = verdict.filtered(None, "m", (apron,))
    assert [(x["input_hash"], x["prefilter"]) for x in got] == [
        ("a", "keyword:title:apron"),
        ("d", "keyword:title:apron"),
    ]
    assert [x["input_hash"] for x in verdict.filtered(None, "m", (apron,), (linen,))] == [
        "a"
    ]  # d: already excluded
