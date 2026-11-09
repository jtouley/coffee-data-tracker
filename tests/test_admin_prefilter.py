"""The admin's keyword filters (the Queue's Keyword filters section): stored in the control plane (append-only,
versioned, audited in the write's transaction, idempotent, undoable), previewed against the served release before
they can be saved, carried to the build by the draft's snapshot, and gated at Publish by CLAUDE.md step 9: at 50 or
fewer public coffees changed, the short path; over 50, the draft build's eval_prefilter and the owner's review of
every product Claude calls coffee."""

import json

import pyarrow as pa
import pytest

from admin import writes as w
from coffeedata import decisions
from coffeedata.lake import SCHEMAS, Lake

from .test_admin import B1, W1, W3, _ops, build_lake, tree
from .test_admin_actions import _draft_build, audit, make, post

# The served build B1's products. "apron" on titles newly excludes c1 (a public coffee Claude calls coffee) and x2
# (not public; Claude agrees); not x3 (the code already excludes a mug), x4 (a longer word), x5 (a hyphenated word)
# or x6 (the owner's include settles it).
PRODUCTS = [
    ("c1", "good.example", "Guji Apron", "Coffee", ["single origin"], "coffee", "verdict:coffee"),
    ("c2", "good.example", "Huila", "Coffee", ["linen"], "coffee", "verdict:coffee"),
    ("x2", "other.example", "Linen Apron", "Kitchen", ["linen"], "not_coffee", "verdict:not_coffee"),
    ("x3", "other.example", "Apron Mug", "", [], None, "excluded:not_coffee_words"),
    ("x4", "other.example", "Aproned Blend", "", [], "coffee", "verdict:coffee"),
    ("x5", "other.example", "Kitchen half-apron", "", [], "not_coffee", "verdict:not_coffee"),
    ("x6", "other.example", "Apron Roast", "", [], "coffee", "decision:include"),
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
    assert (p["count"], p["public"], p["claude_coffee"], p["over"]) == (2, 1, 1, False)
    assert not p["refusals"] and p["token"]
    assert p["lines"][0] == (
        f"“apron” in titles takes 2 products out of the served release {B1}: 1 public coffee "
        "leaves the catalog. Claude calls 1 of them coffee."
    )
    assert any("50 or fewer" in x for x in p["lines"])
    # Claude's coffee first, with its link and store; then a sample of the rest (no product twice)
    titles = [[i["title"] for i in g["items"]] for g in p["groups"]]
    assert titles == [["Guji Apron"], ["Linen Apron"]]
    assert p["groups"][0]["items"][0]["url"] == "https://good.example/p/c1"
    # a filter on tags; nothing matched; a bad phrase
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
    p, r = _add(c)
    assert r.status_code == 200 and r.json()["event"] == "added"
    again = post(c, "/api/prefilter", {"key": "a1", "event": "add", "phrase": "apron", "field": "title",
                                       "based_on": "", "reason": "aprons are not coffee", "previewed": p["token"]})  # fmt: skip
    assert again.json() == r.json()  # a double submit answers once
    kf = app.state.writes.control.keyword_filters()
    assert [(f["phrase"], f["field"]) for f in kf["filters"]] == [("apron", "title")]
    # the page's version is now stale: a second tab's save is a conflict, and a duplicate is refused
    assert post(c, "/api/prefilter/check", {"key": "x", "event": "add", "phrase": "linen", "field": "tags",
                                            "based_on": ""}).json()["refusals"]  # fmt: skip
    dup = post(c, "/api/prefilter/check", {"key": "x", "event": "add", "phrase": "APRON", "field": "title",
                                           "based_on": str(kf["version"])}).json()  # fmt: skip
    assert any("already a filter" in x for x in dup["refusals"])
    with pytest.raises(Exception, match="changed since"):
        app.state.writes.control.change_filter("owner", "k9", "added", "linen", "tags", "linen", None)
    rows = [x for x in audit(app) if x["action"] == "keyword_filter"]
    assert (
        len(rows) == 1
        and rows[0]["subject"] == "title:apron"
        and rows[0]["detail"]["reason"] == "aprons are not coffee"
    )
    page = c.get("/part/queue").text
    assert (
        "Keyword filters" in page and "“apron”" in page and "Remove…" in page and "Preview and add…" in page
    )
    assert '<td class="n">4</td>' in page  # every match in the served build: c1, x2, x3, x6
    assert "settings-doc" not in page and "JSON" not in page  # the JSON editor is gone


def test_undo_reverts_the_latest_change_to_a_filter(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    _, r = _add(c)
    change = r.json()["filter_change"]
    u = post(c, f"/api/prefilter/{change}/undo", {"key": "u1"})
    assert u.json()["undone"] is True and not app.state.writes.control.keyword_filters()["filters"]
    assert post(c, f"/api/prefilter/{change}/undo", {"key": "u1"}).json() == u.json()  # idempotent
    assert (
        post(c, f"/api/prefilter/{change}/undo", {"key": "u2"}).json()["undone"] is False
    )  # superseded by the undo
    undo_of_undo = post(c, f"/api/prefilter/{u.json()['filter_change']}/undo", {"key": "u3"}).json()
    assert undo_of_undo["undone"] and app.state.writes.control.keyword_filters()["filters"]
    assert post(c, "/api/prefilter/999999/undo", {"key": "u4"}).status_code == 400
    assert {x["action"] for x in audit(app)} >= {"keyword_filter", "keyword_filter_undo"}
    hist = app.state.writes.control.filter_history()
    assert [h["event"] for h in hist] == ["added", "removed", "added"] and hist[0]["latest"]


def test_removing_a_filter_needs_its_preview_too(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    _add(c)
    v = str(app.state.writes.control.keyword_filters()["version"])
    p, r = _add(c, event="remove", key="r1", based_on=v, reason="too broad")
    assert p["lines"][0] == (
        "Removing “apron” in titles gives 2 products back to Claude and the rules. Claude calls "
        "1 of them coffee: it comes back to the catalog."
    )
    assert r.status_code == 200 and not app.state.writes.control.keyword_filters()["filters"]
    gone = post(c, "/api/prefilter/check", {"key": "x", "event": "remove", "phrase": "apron", "field": "title",
                                            "based_on": str(r.json()["filter_change"])}).json()  # fmt: skip
    assert any("isn't a filter" in x for x in gone["refusals"])


def _eval(lake_root, build, coffee=("c1",), affected=2):
    rows = [{"build_id": build, "kind": "summary", "affected": affected, "claude_not_coffee": affected - len(coffee),
             "claude_coffee": len(coffee), "no_verdict": 0, "share_not_coffee": 0.5, "wilson_low": 0.09,
             "wilson_high": 0.91}]  # fmt: skip
    rows += [{"build_id": build, "kind": "product", "coffee_id": cid, "roaster_id": "good.example", "title": "Guji Apron",
              "url": "https://good.example/p/c1", "input_hash": f"h-{cid}", "prefilter": "keyword:title:apron",
              "verdict_label": "coffee"} for cid in coffee]  # fmt: skip
    Lake(str(lake_root)).write_ops(
        "eval_prefilter", W3, build, pa.Table.from_pylist(rows, SCHEMAS["eval_prefilter"])
    )


def _drafted(tmp_path, c, lake, fake):
    _add(c)
    d = post(c, "/api/drafts", {"key": "d1"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    fake.finish()
    return d, doc, _draft_build(lake.root, d["draft_id"], doc)  # its release_changes: c1 leaves public_coffee


def test_a_draft_carries_the_filters_and_publish_takes_the_short_path_at_50_or_fewer(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url)
    _add(c)
    assert "1 keyword filter not in it" in c.get("/part/release").text  # the draft-to-be names it
    assert "The keyword filters change: title:apron" in c.get("/part/release").text
    d = post(c, "/api/drafts", {"key": "d1"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    fake.finish()
    b3 = _draft_build(lake.root, d["draft_id"], doc)
    assert doc["prefilter"] == [{"phrase": "apron", "field": "title"}]  # the build gets it from the snapshot
    assert "already builds them" in c.get("/part/release").text  # the same filters: the draft that has them
    _eval(lake.root, b3)
    page = c.get(f"/part/release?build={b3}").text
    assert "Keyword filters (CLAUDE.md step 9)" in page and "within step 9’s 50" in page
    assert "2 products newly excluded, of which 1 Claude calls coffee" in page and "Guji Apron" in page
    assert "marking is optional" in page
    r = post(c, f"/api/drafts/{d['draft_id']}/publish", {"key": "p1", "reason": "aprons out"})
    assert r.status_code == 200, r.json()
    # the step-9 running total counts the one public coffee the filter moved
    assert app.state.writes.control.step9_total({b3})[0] == 1


def test_over_50_publish_waits_for_the_owners_review_of_each_product_claude_calls_coffee(
    tmp_path, lake, pg_url, monkeypatch
):
    monkeypatch.setattr(w, "STEP9_LIMIT", 0)  # one public coffee moves in this lake: over the (lowered) bar
    app, c, fake = make(tmp_path, lake.root, pg_url)
    d, doc, b3 = _drafted(tmp_path, c, lake, fake)
    did = d["draft_id"]
    no = post(c, f"/api/drafts/{did}/publish", {"key": "p1", "reason": "go"})
    assert no.status_code == 409 and any("no evaluation of the filters" in x for x in no.json()["reasons"])
    _eval(lake.root, b3)
    page = c.get(f"/part/release?build={b3}").text
    assert "Publish waits until you mark each product below" in page
    assert "Keep excluded" in page and "Drop the filter" in page and "1 still to review" in page
    no = post(c, f"/api/drafts/{did}/publish", {"key": "p2", "reason": "go"})
    assert no.status_code == 409 and any("1 still to review" in x for x in no.json()["reasons"])
    # only a listed product can be marked
    bad = post(
        c,
        "/api/prefilter/reviews",
        {"key": "m0", "draft_id": did, "coffee_id": "c2", "mark": "keep_excluded"},
    )
    assert bad.status_code == 400
    drop = post(
        c, "/api/prefilter/reviews", {"key": "m1", "draft_id": did, "coffee_id": "c1", "mark": "drop_filter"}
    )
    assert drop.status_code == 200
    no = post(c, f"/api/drafts/{did}/publish", {"key": "p3", "reason": "go"})
    assert no.status_code == 409 and any("drop the filter" in x for x in no.json()["reasons"])
    post(
        c,
        "/api/prefilter/reviews",
        {"key": "m2", "draft_id": did, "coffee_id": "c1", "mark": "keep_excluded"},
    )
    ok = post(c, f"/api/drafts/{did}/publish", {"key": "p4", "reason": "aprons out"})
    assert ok.status_code == 200, ok.json()
    # measured on its own, the filter change isn't added to the running total
    assert app.state.writes.control.step9_total({b3})[0] == 0
    marks = [x for x in audit(app) if x["action"] == "prefilter_review"]
    assert [x["detail"]["mark"] for x in marks] == ["keep_excluded", "drop_filter"]  # newest first
    assert w.PREFILTER_UNREVIEWED_MAX == 0  # a constant in code, not a setting


def test_with_nothing_served_there_is_nothing_to_preview(tmp_path, pg_url):
    from .test_admin import _release  # noqa: F401 - the empty lake below has no release at all

    root = tmp_path / "empty"
    root.mkdir()
    app, c, _ = make(tmp_path, root, pg_url)
    p = post(
        c, "/api/prefilter/check", {"key": "p", "event": "add", "phrase": "apron", "field": "title"}
    ).json()
    assert p["refusals"] and "Nothing is served" in p["lines"][0]
