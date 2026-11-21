"""The admin's keyword filters (its Filters page): stored in the control plane (append-only, versioned, audited in
the write's transaction, idempotent, undoable through the same preview), previewed against the served release before
they can be saved, carried to the build by the draft's snapshot, and gated at Publish by CLAUDE.md step 9: every
public coffee they move counts toward the running total; a new filter over 50 waits until the owner has marked each
product it takes out that Claude calls coffee or hasn't seen; a removal never waits."""

import json

import pyarrow as pa
import pytest

from admin import writes as w
from coffeedata import classify, decisions
from coffeedata.lake import SCHEMAS, Lake

from .test_admin import B1, W1, W3, _ops, build_lake, tree
from .test_admin_actions import _draft_build, audit, make, post

# The served build B1's products. "apron" on titles newly excludes c1 (a public coffee Claude calls coffee), x2 (not
# public; Claude calls it not coffee) and x7 (not public; no verdict yet); not x3 (the code already excludes a mug),
# x4 (a longer word), x5 (a hyphenated word) or x6 (the owner's include settles it).
PRODUCTS = [
    ("c1", "good.example", "Guji Apron", "Coffee", ["single origin"], "coffee", "verdict:coffee"),
    ("c2", "good.example", "Huila", "Coffee", ["linen"], "coffee", "verdict:coffee"),
    ("x2", "other.example", "Linen Apron", "Kitchen", ["linen"], "not_coffee", "verdict:not_coffee"),
    ("x3", "other.example", "Apron Mug", "", [], None, "excluded:not_coffee_words"),
    ("x4", "other.example", "Aproned Blend", "", [], "coffee", "verdict:coffee"),
    ("x5", "other.example", "Kitchen half-apron", "", [], "not_coffee", "verdict:not_coffee"),
    ("x6", "other.example", "Apron Roast", "", [], "coffee", "decision:include"),
    ("x7", "new.example", "Waxed Apron", "", [], None, "signals:1"),
]


@pytest.fixture
def lake(tmp_path):
    lake = build_lake(tmp_path / "lake")
    _ops(Lake(str(lake.root)), W1, B1, "product_decisions", [
        {"coffee_id": cid, "roaster_id": rid, "product_id": cid, "title": t, "product_type": pt, "tags": tags,
         "url": f"https://{rid}/p/{cid}", "is_listed": True, "input_hash": f"h-{cid}", "is_coffee": label == "coffee",
         "classified_by": "claude", "verdict_label": label, "reason": why}
        for cid, rid, t, pt, tags, label, why in PRODUCTS
    ])  # fmt: skip
    return lake


def _add(
    c, phrase="apron", field="title", key="a1", based_on="", event="add", reason="aprons are not coffee"
):
    body = {"key": key, "event": event, "phrase": phrase, "field": field, "based_on": based_on}
    preview = post(c, "/api/prefilter/check", body)
    assert preview.status_code == 200, preview.json()
    return preview.json(), post(
        c, "/api/prefilter", body | {"reason": reason, "previewed": preview.json()["token"]}
    )


def test_the_preview_counts_what_a_filter_newly_excludes_in_the_served_release(tmp_path, lake, pg_url):
    before = tree(lake.root)
    app, c, _ = make(tmp_path, lake.root, pg_url)
    p = post(
        c, "/api/prefilter/check", {"key": "p", "event": "add", "phrase": " Apron ", "field": "title"}
    ).json()
    assert (p["count"], p["public"], p["claude_coffee"], p["unseen"], p["over"]) == (3, 1, 1, 1, False)
    assert not p["refusals"] and p["token"]
    # plain words first: what leaves the catalog, then the products grouped by Claude's call, each with its store
    assert p["lines"] == ["Removes 1 product from the catalog.", "Check each one below."]
    assert [(g["title"], [i["title"] for i in g["items"]], bool(g.get("details"))) for g in p["groups"]] == [
        ("Claude calls these coffee (1)", ["Guji Apron"], False),
        ("Also matched, already out of the catalog", ["Waxed Apron", "Linen Apron"], True),  # under Details
    ]
    assert (
        p["groups"][0]["call"] == "coffee"
        and p["groups"][0]["items"][0]["url"] == "https://good.example/p/c1"
    )
    assert p["details"][0] == f"Checked against the served release {B1}."
    assert "matches 3 products" in p["details"][1] and "hasn’t decided 1" in p["details"][1]
    assert "CLAUDE.md step 9" in p["details"][2]
    assert not any("CLAUDE.md" in x or B1 in x for x in p["lines"])
    t = post(
        c, "/api/prefilter/check", {"key": "p", "event": "add", "phrase": "linen", "field": "tags"}
    ).json()
    assert (t["count"], t["public"]) == (2, 1)  # c2 (public) and x2
    none = post(
        c, "/api/prefilter/check", {"key": "p", "event": "add", "phrase": "espresso cup", "field": "title"}
    )
    assert none.json()["count"] == 0 and not none.json()["groups"]
    assert (
        post(
            c, "/api/prefilter/check", {"key": "p", "event": "add", "phrase": "a(", "field": "title"}
        ).status_code
        == 400
    )
    assert not app.state.writes.control.keyword_filters()["filters"]  # a preview saves nothing
    assert tree(lake.root) == before  # the admin never writes the lake


def test_the_preview_reads_text_as_the_build_does(tmp_path, pg_url):
    """The preview finds what the build excludes, through the filters' own normalization: case folding ("Straße"
    reads "strasse"), full-width letters; and it treats typographic punctuation as part of a word, as the build
    does (M3, M4)."""
    lake = build_lake(tmp_path / "lake")
    rows = [("u1", "Straße Espresso Kit"), ("u2", "ＡＰＲＯＮ Classic"), ("u3", "Cold‐brew Blend"), ("u4", "Brew’d Awakening"),
            ("u5", "Brew Kit")]  # fmt: skip
    _ops(Lake(str(lake.root)), W1, B1, "product_decisions", [
        {"coffee_id": cid, "roaster_id": "u.example", "product_id": cid, "title": t, "product_type": "", "tags": [],
         "url": f"https://u.example/p/{cid}", "is_listed": True, "input_hash": f"h-{cid}", "is_coffee": True,
         "classified_by": "claude", "verdict_label": "coffee", "reason": "verdict:coffee"} for cid, t in rows])  # fmt: skip
    _, c, _ = make(tmp_path, lake.root, pg_url)

    def titles(phrase):
        p = post(
            c, "/api/prefilter/check", {"key": "p", "event": "add", "phrase": phrase, "field": "title"}
        ).json()
        return sorted(i["title"] for g in p["groups"] for i in g["items"])

    assert titles("strasse") == ["Straße Espresso Kit"]
    assert titles("apron classic") == ["ＡＰＲＯＮ Classic"]
    assert titles("brew") == [
        "Brew Kit"
    ]  # not "Cold‐brew" (a Unicode hyphen) nor "Brew’d" (a curly apostrophe)


def test_a_filter_is_saved_only_after_its_preview_and_is_versioned_audited_idempotent(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    body = {
        "key": "s1",
        "event": "add",
        "phrase": "apron",
        "field": "title",
        "based_on": "",
        "reason": "aprons",
    }
    assert post(c, "/api/prefilter", body).status_code == 400  # no preview
    stale = post(c, "/api/prefilter/check", body | {"phrase": "linen"}).json()["token"]
    assert (
        "Preview" in post(c, "/api/prefilter", body | {"previewed": stale}).json()["detail"]
    )  # another change's
    # the token is keyed: the unkeyed digest of the same change doesn't pass (M1)
    import hashlib

    forged = hashlib.sha256(json.dumps(["added", "title", "apron", None, B1]).encode()).hexdigest()[:32]
    assert post(c, "/api/prefilter", body | {"previewed": forged}).status_code == 400
    writes = app.state.writes
    f = classify.KeywordFilter("apron", "title")
    assert writes._token("owner", "added", f, None, B1) != writes._token("someone", "added", f, None, B1)
    assert writes._token("owner", "added", f, None, B1) != writes._token(
        "owner", "added", f, None, "other-build"
    )
    p, r = _add(c)
    assert r.status_code == 200 and r.json()["event"] == "added"
    again = post(c, "/api/prefilter", {"key": "a1", "event": "add", "phrase": "apron", "field": "title",
                                       "based_on": "", "reason": "aprons are not coffee", "previewed": p["token"]})  # fmt: skip
    assert again.json() == r.json()  # a double submit answers once
    kf = writes.control.keyword_filters()
    assert [(x["phrase"], x["field"]) for x in kf["filters"]] == [("apron", "title")]
    assert post(c, "/api/prefilter/check", {"key": "x", "event": "add", "phrase": "linen", "field": "tags",
                                            "based_on": ""}).json()["refusals"]  # fmt: skip
    dup = post(c, "/api/prefilter/check", {"key": "x", "event": "add", "phrase": "APRON", "field": "title",
                                           "based_on": str(kf["version"])}).json()  # fmt: skip
    assert any("already a filter" in x for x in dup["refusals"])
    with pytest.raises(Exception, match="changed since"):
        writes.control.change_filter("owner", "k9", "added", "linen", "tags", "linen", None)
    rows = [x for x in audit(app) if x["action"] == "keyword_filter"]
    assert (
        len(rows) == 1
        and rows[0]["subject"] == "title:apron"
        and rows[0]["detail"]["reason"] == "aprons are not coffee"
    )
    page = c.get("/part/filters").text
    # each filter counts only what it newly excludes: c1, x2, x7; not the mug (code) nor the owner's include (M6)
    # not published yet: it takes nothing out until a draft carrying it is (M4); it would take out c1, x2, x7 (M6)
    assert "1 filter (1 in draft) · 0 products excluded" in page and "in draft</span>" in page
    assert "would take out 3" in page
    assert "“apron”" in page and "Remove…" in page and "Add filter" in page and "Preview…" in page
    assert "none sampled yet" in page
    assert "/filters" in c.get("/queue").text  # its own page in the nav
    queue = c.get("/part/queue").text
    assert "settings-doc" not in queue and "JSON" not in queue and "kf-phrase" not in queue


def _undo(c, change, event, based_on, key="u1"):
    body = {"key": key, "event": event, "phrase": "apron", "field": "title", "based_on": str(based_on or "")}
    p = post(c, "/api/prefilter/check", body).json()
    return p, post(c, f"/api/prefilter/{change}/undo", body | {"previewed": p.get("token")})


def test_undo_goes_through_the_preview_the_version_and_the_cap(tmp_path, lake, pg_url, monkeypatch):
    """H3: an undo is previewed like any change (the reverse change: an add is undone by a removal), checked against
    the version the page showed, and kept within the filters' cap."""
    app, c, _ = make(tmp_path, lake.root, pg_url)
    cp = app.state.writes.control
    _, r = _add(c)
    change = r.json()["filter_change"]
    v = cp.keyword_filters()["version"]
    no = post(c, f"/api/prefilter/{change}/undo", {"key": "u0", "event": "remove", "phrase": "apron", "field": "title",
                                                   "based_on": str(v)})  # fmt: skip
    assert no.status_code == 400 and "Preview" in no.json()["detail"]  # no preview, no undo
    wrong = post(c, f"/api/prefilter/{change}/undo", {"key": "u0", "event": "add", "phrase": "apron", "field": "title",
                                                      "based_on": str(v)})  # fmt: skip
    assert wrong.status_code == 400 and "reverse" in wrong.json()["detail"]
    p, u = _undo(c, change, "remove", v)
    # the undo's preview: the filter was never published, so undoing its add puts nothing back (M4)
    assert p["lines"][:2] == [
        "Puts nothing back in the catalog.",
        "“apron” in titles isn’t published yet: removing it only takes it out of the next draft.",
    ]
    assert p["public"] == 0 and p["in_all"] == 0
    assert u.json()["undone"] is True and not cp.keyword_filters()["filters"]
    assert post(c, f"/api/prefilter/{change}/undo", {"key": "u1"}).json() == u.json()  # idempotent on its key
    # a stale page's undo is a conflict
    with pytest.raises(Exception, match="changed since"):
        cp.undo_filter("owner", "u9", u.json()["filter_change"], v)
    # the cap: with the filters full, undoing a removal (adding the filter back) is refused
    monkeypatch.setattr(classify, "KEYWORD_MAX", 1)
    _add(c, phrase="linen", key="a2", based_on=str(cp.keyword_filters()["version"]))
    with pytest.raises(ValueError, match="at most 1"):
        cp.undo_filter("owner", "u8", u.json()["filter_change"], cp.keyword_filters()["version"])
    assert {x["action"] for x in audit(app)} >= {"keyword_filter", "keyword_filter_undo"}
    hist = cp.filter_history()
    assert [h["event"] for h in hist] == ["added", "removed", "added"] and hist[0]["latest"]
    assert "Undo…" in c.get("/part/filters").text  # Recent changes offers it, through the preview


def _serve(lake_root, filters=(("apron", "title"),)):
    """The served build B1's snapshot carries these keyword filters, as a published draft's would."""
    from datetime import UTC, date, datetime

    doc = decisions.snapshot("served-kf", "2026-10-19T00:00:00Z", [],
                             keyword_filters=[{"phrase": ph, "field": fl} for ph, fl in filters])  # fmt: skip
    Lake(str(lake_root)).write("builds", f"builds/run_date={W1}/build={B1}-snapshot.parquet", [
        {"build_id": B1, "built_at": datetime(2026, 10, 20, 3, tzinfo=UTC), "run_date": date.fromisoformat(W1),
         "kind": "snapshot", "unique_id": "served-kf", "checksum": decisions.digest(doc),
         "payload": json.dumps(doc, sort_keys=True)}])  # fmt: skip


def test_removing_an_unpublished_filter_puts_nothing_back(tmp_path, lake, pg_url):
    """M4: against the served release, a filter only in the control plane has taken nothing out."""
    app, c, _ = make(tmp_path, lake.root, pg_url)
    _add(c)
    v = str(app.state.writes.control.keyword_filters()["version"])
    p = post(c, "/api/prefilter/check", {"key": "r", "event": "remove", "phrase": "apron", "field": "title",
                                         "based_on": v}).json()  # fmt: skip
    assert p["lines"][0] == "Puts nothing back in the catalog." and "isn’t published yet" in p["lines"][1]
    assert (p["count"], p["public"], p["in_all"]) == (0, 0, 0) and not p["groups"]


def test_removing_a_filter_counts_what_comes_back_and_never_waits(tmp_path, lake, pg_url):
    _serve(lake.root)
    app, c, _ = make(tmp_path, lake.root, pg_url)
    assert "1 filter · " not in c.get("/part/filters").text  # the control plane has none yet
    _add(c)
    assert "1 filter · 3 products excluded" in c.get("/part/filters").text  # published: it takes them out
    v = str(app.state.writes.control.keyword_filters()["version"])
    p, r = _add(c, event="remove", key="r1", based_on=v, reason="too broad")
    assert p["lines"] == ["Puts 1 product back in the catalog.",
                          "Removing a filter only hands products back to Claude and the rules, so it never waits for "
                          "a review."]  # fmt: skip
    assert p["public"] == 1 and not p["over"]
    assert r.status_code == 200 and not app.state.writes.control.keyword_filters()["filters"]
    gone = post(c, "/api/prefilter/check", {"key": "x", "event": "remove", "phrase": "apron", "field": "title",
                                            "based_on": str(r.json()["filter_change"])}).json()  # fmt: skip
    assert any("isn't a filter" in x for x in gone["refusals"])


def test_a_removal_counts_seen_and_unseen_products_the_same_way_its_lists_do(tmp_path, pg_url):
    """What a removal puts back, by Claude's call: one Claude calls coffee, one it hasn't seen that the rules call
    coffee; the two that come back not coffee (one seen, one not) aren't put back. The lines count within what's put
    back, as the lists below them do, so "hasn't seen" never contradicts "Claude calls these coffee"."""
    lake = build_lake(tmp_path / "lake")
    rows = [("r1", "Guji Apron", "coffee", True), ("r2", "Apron Espresso", None, True),
            ("r3", "Waxed Apron", None, False), ("r4", "Linen Apron", "not_coffee", False)]  # fmt: skip
    _ops(Lake(str(lake.root)), W1, B1, "product_decisions", [
        {"coffee_id": cid, "roaster_id": "m.example", "product_id": cid, "title": t, "product_type": "", "tags": [],
         "url": f"https://m.example/p/{cid}", "is_listed": True, "input_hash": f"h-{cid}", "is_coffee": False,
         "classified_by": "rules", "verdict_label": label, "reason": "excluded:keyword:title:apron",
         "unfiltered_is_coffee": unfiltered} for cid, t, label, unfiltered in rows])  # fmt: skip
    _serve(lake.root)
    app, c, _ = make(tmp_path, lake.root, pg_url)
    _add(c)
    v = str(app.state.writes.control.keyword_filters()["version"])
    p = post(c, "/api/prefilter/check", {"key": "r", "event": "remove", "phrase": "apron", "field": "title",
                                         "based_on": v}).json()  # fmt: skip
    assert p["lines"] == ["Puts 2 products back in the catalog.",
                          "Removing a filter only hands products back to Claude and the rules, so it never waits for "
                          "a review.", "Claude hasn’t decided 1 of them yet: the rules decide it until it does."]  # fmt: skip
    groups = [(g["title"], [i["title"] for i in g["items"]], bool(g.get("details"))) for g in p["groups"]]
    assert groups == [("Claude calls these coffee (1)", ["Guji Apron"], False),
                      ("Claude hasn’t decided these (1)", ["Apron Espresso"], False),
                      ("Also given back, not coffee", ["Linen Apron", "Waxed Apron"], True)]  # fmt: skip
    assert p["public"] == 2


def test_past_the_cap_a_product_keeps_its_label(tmp_path, lake, pg_url, monkeypatch):
    """M3: a moved product the list's cap cuts is counted under its own group ("and 1 more"), never filed under
    "Also given back, not coffee"."""
    monkeypatch.setattr(w, "PREFILTER_LIST_MAX", 0)
    _serve(lake.root)
    app, c, _ = make(tmp_path, lake.root, pg_url)
    _add(c)
    v = str(app.state.writes.control.keyword_filters()["version"])
    p = post(c, "/api/prefilter/check", {"key": "r", "event": "remove", "phrase": "apron", "field": "title",
                                         "based_on": v}).json()  # fmt: skip
    coffee = next(g for g in p["groups"] if g["title"] == "Claude calls these coffee (1)")
    assert coffee["items"] == [] and coffee["more"] == 1
    rest = [i["title"] for g in p["groups"] if g.get("details") for i in g["items"]]
    assert "Guji Apron" not in rest and sorted(rest) == ["Linen Apron", "Waxed Apron"]


def test_the_weighted_interval_follows_the_stores():
    from admin.queries import wilson, wilson_weighted

    assert wilson_weighted([(5, 2, 1)])[1:] == wilson(1, 2)  # one stratum: plain Wilson
    share, lo, hi = wilson_weighted([(900, 1, 1), (1, 1, 0), (1, 1, 0)])
    assert share > 0.99 and lo > 0.1  # the big store decides the estimate, the small ones don't outvote it
    assert wilson_weighted([(9, 0, 0)]) == (0.0, 0.0, 1.0)  # nothing answered: no estimate


def _eval(lake_root, build, rows=(("c1", "coffee", "coffee"), ("x2", "not_coffee", "not_coffee"),
                                  ("x7", None, "no_verdict")), restored=(), hashes=None):  # fmt: skip
    """The draft build's census, as eval_prefilter and eval_prefilter_summary write it."""
    w = Lake(str(lake_root))
    prod = [{"build_id": build, "direction": "excluded", "coffee_id": cid, "roaster_id": "good.example",
             "title": f"{cid} Apron", "url": f"https://good.example/p/{cid}",
             "input_hash": (hashes or {}).get(cid, f"h-{cid}"),
             "prefilter": "keyword:title:apron", "verdict_label": label, "claude": call, "is_coffee_after": False}
            for cid, label, call in rows]  # fmt: skip
    prod += [{"build_id": build, "direction": "restored", "coffee_id": cid, "roaster_id": "good.example",
              "title": cid, "url": None, "input_hash": f"h-{cid}", "prefilter": "keyword:title:linen",
              "verdict_label": None, "claude": "no_verdict", "is_coffee_after": True} for cid in restored]  # fmt: skip
    w.write_ops("eval_prefilter", W3, build, pa.Table.from_pylist(prod, SCHEMAS["eval_prefilter"]))

    def count(direction):
        xs = [x for x in prod if x["direction"] == direction]
        return {"build_id": build, "direction": direction, "products": len(xs),
                "claude_coffee": sum(x["claude"] == "coffee" for x in xs),
                "claude_not_coffee": sum(x["claude"] == "not_coffee" for x in xs),
                "no_verdict": sum(x["claude"] == "no_verdict" for x in xs),
                "back_in_catalog": sum(bool(x["is_coffee_after"]) for x in xs)}  # fmt: skip

    w.write_ops("eval_prefilter_summary", W3, build,
                pa.Table.from_pylist([count("excluded"), count("restored")], SCHEMAS["eval_prefilter_summary"]))  # fmt: skip


def _drafted(tmp_path, c, lake, fake, phrase="apron", event="add", based_on=""):
    _add(c, phrase=phrase, event=event, based_on=based_on, key=f"a-{phrase}-{event}")
    d = post(c, "/api/drafts", {"key": f"d-{phrase}-{event}"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    fake.finish()
    return d, doc, _draft_build(lake.root, d["draft_id"], doc)  # its release_changes: c1 leaves public_coffee


def test_a_draft_carries_the_filters_and_publish_takes_the_short_path_at_50_or_fewer(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url)
    _add(c)
    page = c.get("/part/release").text
    assert (
        "1 keyword filter not in it" in page and "The keyword filters change: adds “apron” in titles." in page
    )
    d = post(c, "/api/drafts", {"key": "d1"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    fake.finish()
    b3 = _draft_build(lake.root, d["draft_id"], doc)
    assert doc["prefilter"] == [{"phrase": "apron", "field": "title"}]  # the build gets it from the snapshot
    _eval(lake.root, b3)
    page = c.get(f"/part/release?build={b3}").text
    assert "Keyword filters checked against Claude." in page and "a small change (1 coffee leaves" in page
    assert (
        "3 products taken out by keyword filters · Claude calls 1 coffee and 1 not coffee, hasn’t decided 1"
        in page
    )
    assert "marking these is optional" in page and "Claude has a verdict" not in page
    # one public coffee moves, and Change size says so: never 0 beside "1 public row leaves" (M2)
    assert "small: this draft changes 1 public coffee (all through keyword filters)" in page
    assert "1 public row leaves" in page
    fold = page.split('<summary class="btn text start">Details</summary>', 1)
    assert (
        len(fold) == 2 and "Takes out 3 products" in fold[1] and "Wilson" not in page and "agree" not in page
    )
    r = post(c, f"/api/drafts/{d['draft_id']}/publish", {"key": "p1", "reason": "aprons out"})
    assert r.status_code == 200, r.json()
    assert app.state.writes.control.step9_total({b3})[0] == 1  # counted in the running total


def test_over_50_publish_waits_for_a_review_of_coffee_and_unseen_products(
    tmp_path, lake, pg_url, monkeypatch
):
    monkeypatch.setattr(w, "STEP9_LIMIT", 0)  # one public coffee moves in this lake: over the (lowered) bar
    app, c, fake = make(tmp_path, lake.root, pg_url)
    d, doc, b3 = _drafted(tmp_path, c, lake, fake)
    did = d["draft_id"]
    no = post(c, f"/api/drafts/{did}/publish", {"key": "p1", "reason": "go"})
    assert no.status_code == 409 and any(
        "didn’t check the filters against Claude" in x for x in no.json()["reasons"]
    )
    _eval(lake.root, b3)
    page = c.get(f"/part/release?build={b3}").text
    assert "A big change: mark each product below" in page
    assert "Claude calls these coffee (1)" in page and "Claude hasn’t decided these (1)" in page
    assert "Needs your review: 2 products (Claude calls 1 coffee, hasn’t decided 1)" in page
    assert "the next draft needs a full evaluation first, until one is recorded" in page  # M5, said plainly
    assert 'st-warn"><i aria-hidden="true"></i>needs review' in page  # waiting, not failed
    assert "Can’t publish yet, waiting on: Keyword filters checked against Claude." in page
    assert "Classify these…" in page  # x7 has no verdict: Claude can be asked
    # the change counts toward the running total even over 50 (M2), and the filters' part is checked above
    assert "this draft changes 1 public coffee (all through keyword filters)" in page
    assert "The keyword filters’ part is checked above" in page
    for cid, mark, key in (("c2", "keep_excluded", "m0"),):  # only a listed product can be marked
        assert post(c, "/api/prefilter/reviews", {"key": key, "draft_id": did, "coffee_id": cid, "input_hash": f"h-{cid}",
                                                  "mark": mark}).status_code == 400  # fmt: skip
    post(c, "/api/prefilter/reviews", {"key": "m1", "draft_id": did, "coffee_id": "c1", "input_hash": "h-c1",
                                       "mark": "keep_excluded"})  # fmt: skip
    no = post(c, f"/api/drafts/{did}/publish", {"key": "p2", "reason": "go"})
    assert no.status_code == 409 and any("Needs your review: 1 product (Claude hasn’t decided 1)" in x
                                         for x in no.json()["reasons"])  # fmt: skip
    drop = post(c, "/api/prefilter/reviews", {"key": "m2", "draft_id": did, "coffee_id": "x7", "input_hash": "h-x7",
                                              "mark": "drop_filter"})  # fmt: skip
    assert drop.status_code == 200
    no = post(c, f"/api/drafts/{did}/publish", {"key": "p3", "reason": "go"})
    assert no.status_code == 409 and any("“Drop the filter”" in x for x in no.json()["reasons"])
    post(c, "/api/prefilter/reviews", {"key": "m3", "draft_id": did, "coffee_id": "x7", "input_hash": "h-x7",
                                       "mark": "keep_excluded"})  # fmt: skip
    assert (
        "you checked each product Claude calls coffee or hasn’t decided"
        in c.get(f"/part/release?build={b3}").text
    )
    ok = post(c, f"/api/drafts/{did}/publish", {"key": "p4", "reason": "aprons out"})
    assert ok.status_code == 200, ok.json()
    assert app.state.writes.control.step9_total({b3})[0] == 1  # still counted in the running total
    marks = [x for x in audit(app) if x["action"] == "prefilter_review"]
    assert [x["detail"]["mark"] for x in marks] == ["keep_excluded", "drop_filter", "keep_excluded"]
    assert w.PREFILTER_UNREVIEWED_MAX == 0  # a constant in code, not a setting


def test_a_mark_is_about_the_version_it_was_made_on(tmp_path, lake, pg_url, monkeypatch):
    """M5: a store that rewrites a listing has a new version, which needs its own mark."""
    monkeypatch.setattr(w, "STEP9_LIMIT", 0)
    app, c, fake = make(tmp_path, lake.root, pg_url)
    d, doc, b3 = _drafted(tmp_path, c, lake, fake)
    _eval(lake.root, b3, rows=(("c1", "coffee", "coffee"),))
    post(c, "/api/prefilter/reviews", {"key": "m1", "draft_id": d["draft_id"], "coffee_id": "c1", "input_hash": "h-c1",
                                       "mark": "keep_excluded"})  # fmt: skip
    assert "you checked each product" in c.get(f"/part/release?build={b3}").text
    digest = w._filters_digest(doc)
    marks = app.state.writes.control.reviews(digest)
    assert ("c1", "h-c1") in marks and ("c1", "h-c1-rewritten") not in marks


def test_only_unseen_products_never_read_as_claude_agreeing(tmp_path, lake, pg_url, monkeypatch):
    """H1: a census with no verdicts at all waits for a review, and Classify these starts the classify job for
    exactly those products, through the Jobs path (its plan, its confirm), from the draft's snapshot."""
    monkeypatch.setattr(w, "STEP9_LIMIT", 0)
    app, c, fake = make(tmp_path, lake.root, pg_url)
    d, doc, b3 = _drafted(tmp_path, c, lake, fake)
    # two products share one version (x9 is c1's): one version to send, as the job reads it (M1)
    _eval(lake.root, b3, rows=(("c1", None, "no_verdict"), ("x9", None, "no_verdict"), ("x7", None, "no_verdict"),
                               ("x8", "unsure", "no_verdict")), hashes={"x9": "h-c1"})  # fmt: skip
    page = c.get(f"/part/release?build={b3}").text
    assert "Claude agrees" not in page and "calls each one not coffee" not in page
    assert "Needs your review: 4 products (Claude hasn’t decided 4)" in page and "(Claude was unsure)" in page
    assert "Asks Claude about the 2 it hasn’t seen" in page
    plan = post(c, f"/api/drafts/{d['draft_id']}/classify/plan", {"key": "q"}).json()
    # the confirm's count is what the job sends: the census's versions with no verdict (not the unsure one)
    assert plan["lines"][0].startswith("Sends 2 products to Claude in one batch")
    assert not plan["refusals"] and f"--prefilter-build {b3}" in plan["details"][0]
    r = post(c, f"/api/drafts/{d['draft_id']}/classify", {"key": "k1"})
    assert r.status_code == 200, r.json()
    run = fake.calls[-1]
    assert run[1] == "build" and f"coffeedata classify --prefilter-build {b3} " in run[2][0]
    assert any(x["action"] == "job_start" and x["subject"] == "classify-review" for x in audit(app))


def test_a_removal_over_50_never_waits(tmp_path, lake, pg_url, monkeypatch):
    monkeypatch.setattr(w, "STEP9_LIMIT", 0)
    app, c, fake = make(tmp_path, lake.root, pg_url)
    # the served release already has "apron": a snapshot that removes it builds against it
    _add(c)
    d1 = post(c, "/api/drafts", {"key": "d1"}).json()
    doc1 = json.loads((tmp_path / "snapshots" / decisions.key(d1["snapshot_id"])).read_text())
    fake.finish()
    b3 = _draft_build(lake.root, d1["draft_id"], doc1)
    _eval(lake.root, b3, rows=(), restored=("c9",))
    effect = __import__("asyncio").run(app.state.writes.effect(
        {"snapshot_id": d1["snapshot_id"], "baseline": B1, "draft_id": d1["draft_id"]}))  # fmt: skip
    assert effect["filters_added"] is True
    gate = __import__("asyncio").run(app.state.writes._prefilter_gate(
        {"snapshot_id": d1["snapshot_id"], "draft_id": d1["draft_id"]}, b3, 60, True, False))  # fmt: skip
    assert gate["check"]["ok"] and "never waits" in gate["check"]["why"]
    assert "Gives back 1 product: 1 come back to the catalog as coffee." in gate["details"]


def test_the_filters_page_shows_the_weekly_check(tmp_path, lake, pg_url):
    """M7: the weekly sample of what the published filters took out, sent to Claude anyway: per filter, how many
    Claude called coffee in the last four weeks, with a Wilson interval."""
    from datetime import UTC, datetime

    app, c, _ = make(tmp_path, lake.root, pg_url)
    _add(c)
    wl = Lake(str(lake.root))
    now = datetime.now(UTC).replace(microsecond=0)
    # one store excluded 9 versions that week, another 1: one sampled from each; the big store's answer weighs 9x
    wl.write_partition("prefilter_samples", W3, "s1", [
        {"sampled_at": now, "run_date": now.date(), "input_hash": h, "roaster_id": r, "prefilter": "keyword:title:apron",
         "model": "m", "definition": "d", "stratum_size": n} for h, r, n in
        (("s1", "big.example", 9), ("s2", "small.example", 1), ("s3", "small.example", 1))])  # fmt: skip
    wl.write_partition("verdicts", W3, "v1", [
        {"decided_at": now, "input_hash": h, "model": "m", "definition": "d", "label": lab}
        for h, lab in (("s1", "coffee"), ("s2", "not_coffee"), ("s3", "unsure"))])  # fmt: skip
    page = c.get("/part/filters").text
    assert "90% called coffee" in page and "2 answered, weighted by store" in page  # unsure: neither


def test_with_nothing_served_there_is_nothing_to_preview(tmp_path, pg_url):
    from .test_admin import _release  # noqa: F401 - the empty lake below has no release at all

    root = tmp_path / "empty"
    root.mkdir()
    app, c, _ = make(tmp_path, root, pg_url)
    p = post(
        c, "/api/prefilter/check", {"key": "p", "event": "add", "phrase": "apron", "field": "title"}
    ).json()
    assert p["refusals"] and "Nothing is served" in p["lines"][0]
