"""The admin's rules and flags (docs/design/admin-registry.md §2.3-3, §5.1): the Queue and a Case read the served
build's mart_flags; settings edits and crawl flags are versioned, checked, audited in the write's transaction and
idempotent; a draft carries them; Publish needs the evaluation of a change to how a rule decides; one live check
covers a group decided on the same evidence."""

import json
from datetime import date

import pyarrow as pa
import pytest

from coffeedata import decisions, rules
from coffeedata.lake import SCHEMAS, Lake

from .test_admin import B1, W1, W3, build_lake, tree
from .test_admin_actions import _draft_build, audit, make, post


def _flag(cid, rule, polarity, family, direction="", roaster="good.example", **kw):
    return {
        "build_id": B1,
        "rule": rule,
        "family": family,
        "polarity": polarity,
        "direction": direction,
        "coffee_id": cid,
        "roaster_id": roaster,
        "input_hash": f"h-{cid}",
        "title": f"<b>{cid}</b>",
        "product_type": "Drink Mix",
        "evidence": json.dumps({"product_type": "Drink Mix", "matched": "drink mix"}),
        "evidence_key": "k",
        "evidence_hash": kw.get("eh", "e1"),
        "decision": None,
        "classifier_is_coffee": kw.get("coffee", True),
        "was_public": kw.get("public", False),
        "held": kw.get("held", False),
        "ready_flags": kw.get("ready", False),
        "coverage_problem": False,
        "supports_families": kw.get("families", 0),
        "doubts": kw.get("doubts", 0),
        "restores": 0,
        "public_rows": kw.get("rows", 1),
        "verdict_label": "coffee",
        "classified_by": "claude",
        "reason": "verdict:coffee",
        "url": kw.get("url", "https://good.example/p"),
        "tags": ["mix"],
        "bag_sizes_g": [340],
        "prices_usd": [12.5],
        "description": kw.get("desc", "Hot cocoa <script>x</script>"),
    }


@pytest.fixture
def lake(tmp_path):
    lake = build_lake(tmp_path / "lake")
    rows = [
        # two drink mixes from two stores, the same evidence: ready, a group
        _flag("d1", "verdict_flip", "supports", "classifier", "added", ready=True, families=2, rows=3),
        _flag("d1", "non_coffee_type", "supports", "listing", ready=True, families=2, rows=3),
        _flag("d2", "verdict_flip", "supports", "classifier", "added", roaster="cf.example", ready=True,
              families=2),
        _flag("d2", "non_coffee_type", "supports", "listing", roaster="cf.example", ready=True, families=2),
        # a held single origin: a classifier removal with pasted text
        _flag("s1", "verdict_flip", "restore", "classifier", "removed", eh="e2", coffee=False, public=True,
              held=True, doubts=1, url="javascript:alert(1)"),
        _flag("s1", "shared_body", "doubts", "catalog", eh="e2", coffee=False, public=True, held=True, doubts=1),
    ]  # fmt: skip
    Lake(str(lake.root)).write_ops("mart_flags", W1, B1, pa.Table.from_pylist(rows, SCHEMAS["mart_flags"]))
    return lake


def test_the_queue_groups_by_evidence_and_a_case_shows_its_evidence(tmp_path, lake, pg_url):
    before = tree(lake.root)
    app, c, _ = make(tmp_path, lake.root, pg_url)
    page = c.get("/part/queue").text
    assert "3 products with open flags · 1 held · 2 ready to remove" in page
    assert "2 products with the same evidence" in page and "Exclude these 2…" in page
    assert page.index("d1") < page.index("s1")  # more public rows first
    assert "1 doubt · 1 restore · held" in page and "2 supports · ready" in page
    assert "&lt;b&gt;d1&lt;/b&gt;" in page and "<b>d1</b>" not in page  # store text is escaped
    assert c.get("/part/queue?state=held").text.count('href="/queue?case=') == 1
    assert "No open flag matches" in c.get("/part/queue?roaster=nobody.example").text
    case = c.get("/part/queue?case=s1").text
    assert "shared_body" in case and "doubts" in case and "the guard keeps it as coffee" in case
    assert "&lt;script&gt;" in case and "<script>x" not in case
    assert "javascript:" not in case  # only https store links
    assert "Live check (test crawl)" in case and 'name="connector" value="shopify"' in case
    assert "No open flags on nope" in c.get("/part/queue?case=nope").text
    assert tree(lake.root) == before  # the admin never writes the lake


def test_a_group_decision_carries_its_evidence_and_one_live_check_covers_it(tmp_path, lake, pg_url):
    """Group approval: two stores' drink mixes decided together on the same evidence; one answered live check of
    either store lets Publish through for both."""
    from coffeedata import control

    app, c, fake = make(tmp_path, lake.root, pg_url)
    items = [{"coffee_id": cid, "input_hash": f"h-{cid}", "roaster_id": r, "title": cid, "evidence_hash": "e1"}
             for cid, r in (("d1", "good.example"), ("d2", "cf.example"))]  # fmt: skip
    r = post(c, "/api/decisions", {"key": "k1", "action": "exclude", "kind": "coffee_id", "reason": "drink mixes",
                                   "items": items, "seen": {}})  # fmt: skip
    assert r.status_code == 200 and r.json()["count"] == 2
    assert "exclude" in c.get("/part/queue").text and "next draft" in c.get("/part/queue").text
    d = post(c, "/api/drafts", {"key": "k2"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    assert {x["evidence_hash"] for x in doc["decisions"]} == {"e1"}
    fake.finish()
    b3 = _draft_build(lake.root, d["draft_id"], doc)
    no = post(c, f"/api/drafts/{d['draft_id']}/publish", {"key": "k3", "reason": "go"})
    assert no.status_code == 409 and any(
        "cf.example" in x and "good.example" in x for x in no.json()["reasons"]
    )
    post(c, "/api/jobs/crawl-one/runs", {"key": "k4", "connector": "shopify", "roaster": "good.example"})
    test_id = app.state.writes.control.runs()[0]["test_id"]
    w = Lake(str(lake.root))
    job = control.start(w, "crawl", W3, "shopify", 1, detail={"test": test_id, "arm": "test"})
    control.finish(w, job, "done", ok=1, errors=0, detail={"test": test_id, "arm": "test"})
    fake.finish()
    page = c.get(f"/part/release?build={b3}").text
    assert "or one of its evidence group did" in page


def test_settings_are_versioned_checked_audited_and_idempotent(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    page = c.get("/part/queue").text
    assert (
        "Rule settings · the defaults" in page
        and "remove_unless_doubted" not in page.split("Rule settings")[0]
    )
    on = rules.default()
    on["guard"]["mode"] = "remove_unless_doubted"
    check = post(c, "/api/settings/check", {"key": "p", "doc": json.dumps(on)}).json()
    assert check["decides"] == ["guard.mode"] and check["needs_evaluation"] == ["guard"]
    assert any("full evaluation" in x for x in check["lines"])
    bad = post(
        c, "/api/settings", {"key": "k0", "doc": json.dumps(on | {"guard": {"mode": "x"}}), "reason": "try"}
    )
    assert bad.status_code == 400 and "guard.mode" in bad.json()["detail"]
    assert post(c, "/api/settings", {"key": "k0", "doc": "{not json", "reason": "try"}).status_code == 400
    r1 = post(
        c,
        "/api/settings",
        {"key": "k1", "doc": json.dumps(on), "reason": "turn the guard on", "based_on": ""},
    )
    r2 = post(
        c,
        "/api/settings",
        {"key": "k1", "doc": json.dumps(on), "reason": "turn the guard on", "based_on": ""},
    )
    assert r1.status_code == 200 and r1.json() == r2.json()  # a double submit acts once
    v1 = r1.json()["version_id"]
    stale = post(
        c, "/api/settings", {"key": "k2", "doc": json.dumps(rules.default()), "reason": "off", "based_on": ""}
    )
    assert stale.status_code == 409  # based on the defaults, but v1 is current: another tab
    same = post(c, "/api/settings", {"key": "k3", "doc": json.dumps(on), "reason": "again", "based_on": v1})
    assert same.status_code == 400 and "nothing changed" in same.json()["detail"]
    saved = [a for a in audit(app) if a["action"] == "settings"]
    assert len(saved) == 1 and saved[0]["detail"]["decides"] == ["guard.mode"]
    assert f"version <code>{v1}</code>" in c.get("/part/queue").text
    # the draft carries the settings, and Publish waits for the guard's evaluation
    d = post(c, "/api/drafts", {"key": "k4"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    assert decisions.settings_of(doc)["guard"]["mode"] == "remove_unless_doubted" and doc["decisions"] == []
    b3 = _draft_build(lake.root, d["draft_id"], doc)
    page = c.get(f"/part/release?build={b3}").text
    assert "Evaluation of a rule change" in page and "eval/guard/" in page
    no = post(c, f"/api/drafts/{d['draft_id']}/publish", {"key": "k5", "reason": "go"})
    assert no.status_code == 409 and any(
        "guard.mode changes how a rule decides" in x for x in no.json()["reasons"]
    )


def test_the_crawl_flag_is_a_versioned_conflict_checked_write(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    assert (
        post(c, "/api/roasters/good.example/crawl", {"key": "k0", "crawl": "off", "reason": ""}).status_code
        == 400
    )
    r = post(c, "/api/roasters/good.example/crawl", {"key": "k1", "crawl": "off", "seen": "on",
                                                     "reason": "sells no coffee"})  # fmt: skip
    assert r.status_code == 200 and r.json() == {"ok": True, "roaster_id": "good.example", "crawl": False}
    again = post(c, "/api/roasters/good.example/crawl", {"key": "k2", "crawl": "off", "seen": "on",
                                                         "reason": "sells no coffee"})  # fmt: skip
    assert again.status_code == 409  # the page showed on: another tab turned it off since
    assert post(
        c, "/api/roasters/x%2F..%2Fy/crawl", {"key": "k3", "crawl": "off", "reason": "bad id"}
    ).status_code in (400, 404)
    case = c.get("/part/queue?case=d1").text
    assert "sells no coffee" in case and "Turn crawling on" in case
    assert [a["subject"] for a in audit(app) if a["action"] == "crawl_flag"] == ["good.example"]
    p = post(c, "/api/drafts", {"key": "k4"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(p["snapshot_id"])).read_text())
    assert decisions.crawl_off(doc) == {"good.example"}
    assert "0 decision changes, 1 crawl flag not in it" in c.get("/part/release").text


def test_a_build_without_flags_says_so(tmp_path, pg_url):
    lake = build_lake(tmp_path / "lake")
    app, c, _ = make(tmp_path, lake.root, pg_url)
    assert "has no flags" in c.get("/part/queue").text
    assert date.fromisoformat(W1)  # the served build is B1
