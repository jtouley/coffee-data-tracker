"""The admin's Sources page (docs/admin/README.md): the source list searched and filtered, per-status and per-tag
counts and the latest discovery run's changes, and the flags: one click, a bulk flag, undo, a stale tab refused,
each audited in its transaction and idempotent, and carried by the next draft's snapshot."""

import json
import re
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from coffeedata import decisions
from coffeedata.lake import Lake

from .test_admin import B1, W1, build_lake, tree
from .test_admin_actions import audit, make, post
from .test_sources import place

RUN1, RUN2 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def lake(tmp_path):
    lake = build_lake(tmp_path / "lake")
    w = Lake(str(lake.root))
    w.write("candidates", "candidates/source=overture/r1.parquet", [
        place("good.example", "Good Coffee", tags=["coffee_roastery"], at=RUN1),
        place("gone.example", "Gone Cafe", at=RUN1),
        place("beans.example", "Beans & Roast", roast=True, at=RUN1),
    ])  # fmt: skip
    w.write("candidates", "candidates/source=overture/r2.parquet", [
        place("good.example", "Good Coffee", tags=["coffee_roastery"], at=RUN2),
        place("beans.example", "Beans & Roast", roast=True, at=RUN2),
        place("<b>new</b>.example", "<b>New</b> Cafe", tags=["coffee_shop"], at=RUN2),
        place("tea.example", "Tea Corner", tags=["coffee_shop"], at=RUN2) | {"website": "javascript:alert(1)"},
    ])  # fmt: skip
    rel = lake.root / "releases" / f"run_date={W1}" / f"build={B1}" / "dim_roaster.parquet"
    pq.write_table(pa.Table.from_pylist([  # the served release's registry: the seed and discovery's roasteries
        {"roaster_id": "good.example", "roaster_name": "Good Coffee", "city": "Bend", "state": "OR",
         "sources": "overture-2026-10"},
        {"roaster_id": "seeded.example", "roaster_name": "Seeded Roasters", "city": "Austin", "state": "TX",
         "sources": "curated"}]), rel)  # fmt: skip
    return lake


def test_the_source_list_its_counts_changes_and_filters(tmp_path, lake, pg_url):
    before = tree(lake.root)
    _, c, _ = make(tmp_path, lake.root, pg_url)
    page = c.get("/part/sources").text
    assert (
        "4 to review of 6 places" in page
        and "2 roasters from the registry · 0 flagged as roasters · 0 flagged as not roasters" in page
    )
    assert (
        "Seeded Roasters" in page and "curated" in page
    )  # a seed roaster, inherited, with where it came from
    assert "2 new, 1 gone" in page and "coffee_shop (2)" in page
    assert "&lt;b&gt;New&lt;/b&gt; Cafe" in page and "<b>New</b>" not in page and "javascript:" not in page
    assert page.index("Beans &amp; Roast") < page.index("Tea Corner")  # a hint first
    assert c.get("/part/sources?status=roaster+(inherited)").text.count("<tr data-row>") == 2
    assert "1 of 6 places" in c.get("/part/sources?origin=curated").text
    assert "1 of 6 places" in c.get("/part/sources?q=tea").text
    assert "1 of 6 places" in c.get("/part/sources?q=https://www.good.example/shop").text  # a pasted address
    assert "2 of 6 places" in c.get("/part/sources?tag=coffee_shop").text
    assert tree(lake.root) == before  # nothing written to the lake, nothing fetched


def test_flags_one_click_bulk_undo_conflict_and_the_draft(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    one = {"key": "k1", "flag": "roaster", "items": [{"roaster_id": "beans.example", "seen": None}]}
    r1, r2 = post(c, "/api/sources/flags", one), post(c, "/api/sources/flags", one)
    assert (
        r1.status_code == 200 and r1.json() == r2.json() and r1.json()["count"] == 1
    )  # a double click acts once
    batch = r1.json()["batch_id"]
    stale = post(c, "/api/sources/flags", one | {"key": "k2", "flag": "not_roaster"})
    assert stale.status_code == 409  # the page showed it unflagged: another tab flagged it since
    bulk = post(c, "/api/sources/flags", {"key": "k3", "flag": "not_roaster", "items": [
        {"roaster_id": "tea.example"}, {"roaster_id": "gone.example"}]})  # fmt: skip
    assert bulk.json()["count"] == 2
    assert "1 flagged as a roaster · 2 flagged as not roasters" in c.get("/part/sources").text
    assert (
        post(
            c, "/api/sources/flags", {"key": "k4", "flag": "maybe", "items": [{"roaster_id": "x.example"}]}
        ).status_code
        == 400
    )
    assert [a["action"] for a in audit(app)][:2] == ["source_flag", "source_flag"]
    # the next draft carries the roasters
    d = post(c, "/api/drafts", {"key": "k5"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    assert decisions.source_roasters(doc) == ["beans.example"]
    # undo puts the place back to unreviewed
    u = post(c, f"/api/sources/flags/{batch}/undo", {"key": "k6"})
    assert u.status_code == 200 and u.json()["reverted"] == ["beans.example"]
    assert "0 flagged as roasters" in c.get("/part/sources").text
    assert post(c, f"/api/sources/flags/{batch}/undo", {"key": "k7"}).json()["count"] == 0  # already undone


def test_a_registry_roaster_marked_not_a_roaster_leaves_the_next_draft(tmp_path, lake, pg_url):
    _, c, _ = make(tmp_path, lake.root, pg_url)
    assert "Roaster (inherited)" in c.get("/part/sources?q=seeded").text  # nothing happens by default
    r = post(
        c,
        "/api/sources/flags",
        {"key": "k1", "flag": "not_roaster", "items": [{"roaster_id": "seeded.example"}]},
    )
    assert r.status_code == 200 and "Not a roaster</span>" in c.get("/part/sources?q=seeded").text
    d = post(c, "/api/drafts", {"key": "k2"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    assert decisions.not_roasters(doc) == ["seeded.example"] and decisions.source_roasters(doc) == []


def test_no_source_list_yet(tmp_path, pg_url):
    lake = build_lake(tmp_path / "lake")
    _, c, _ = make(tmp_path, lake.root, pg_url)
    assert "No places yet" in c.get("/part/sources").text


def test_a_published_place_stays_in_the_next_draft(tmp_path, lake, pg_url):
    """Once a draft adding a place is published, the release's registry lists it: the flag is then already served,
    not a change, and the next draft still carries it (it would otherwise leave the registry again)."""
    doc = decisions.snapshot("s1", "2026-10-19T00:00:00Z", [], sources=["beans.example"])
    Lake(str(lake.root)).write("builds", f"builds/run_date={W1}/build={B1}-snapshot.parquet",
                               [{"build_id": B1, "kind": "snapshot", "unique_id": doc["snapshot_id"],
                                 "checksum": decisions.digest(doc), "payload": json.dumps(doc, sort_keys=True)}])  # fmt: skip
    rel = lake.root / "releases" / f"run_date={W1}" / f"build={B1}" / "dim_roaster.parquet"
    rows = pq.read_table(rel).to_pylist() + [{"roaster_id": "beans.example", "roaster_name": "Beans & Roast",
                                              "city": "Portland", "state": "OR", "sources": "overture-2026-10"}]  # fmt: skip
    pq.write_table(pa.Table.from_pylist(rows), rel)
    _, c, _ = make(tmp_path, lake.root, pg_url)
    post(
        c, "/api/sources/flags", {"key": "k1", "flag": "roaster", "items": [{"roaster_id": "beans.example"}]}
    )
    assert "source flag" not in c.get("/part/release").text  # served already: nothing to build


def test_without_the_flags_no_status_is_shown(tmp_path, lake):
    """The control plane down: the list still reads, but no place says inherited or unreviewed, and no flag counts."""
    from admin.controlplane import ControlPlane, url_connect

    dead = ControlPlane(url_connect("postgresql://nobody@127.0.0.1:1/none"))
    _, c, _ = make(tmp_path, lake.root, "postgresql://nobody@127.0.0.1:1/none", control_plane=dead)
    page = re.sub(
        r'href="[^"]*"', "", c.get("/part/sources?status=unreviewed").text
    )  # the text, not its links
    assert "6 places · the flags can’t be read now" in page and "Seeded Roasters" in page
    for word in ("inherited", "unreviewed", "flagged roaster", "data-write="):
        assert word not in page, word


def test_a_draft_is_refused_while_the_served_registry_cant_be_read(tmp_path, lake, pg_url):
    (lake.root / "releases" / f"run_date={W1}" / f"build={B1}" / "dim_roaster.parquet").unlink()
    _, c, _ = make(tmp_path, lake.root, pg_url)
    post(
        c,
        "/api/sources/flags",
        {"key": "k1", "flag": "not_roaster", "items": [{"roaster_id": "seeded.example"}]},
    )
    assert "registry (dim_roaster) can&#39;t be read" in c.get("/part/release").text
    r = post(c, "/api/drafts", {"key": "k2"})
    assert r.status_code == 409 and "dim_roaster" in r.text  # fails closed: never an empty registry


def test_sort_orders_and_the_panel_count(tmp_path, lake, pg_url):
    _, c, _ = make(tmp_path, lake.root, pg_url)
    page = c.get("/part/sources?sort=name&tag=coffee_shop").text
    assert "Filter / Sort (2)" in page and 'value="name" checked' in page
    names = c.get("/part/sources?sort=name").text
    assert names.index("Beans &amp; Roast") < names.index("Good Coffee") < names.index("Tea Corner")
    assert "Filter / Sort</button>" in c.get("/part/sources?sort=bogus").text  # an unknown order: the default


def test_not_a_roaster_takes_its_coffees_out_and_undo_puts_them_back(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    r = post(
        c,
        "/api/sources/flags",
        {"key": "k1", "flag": "not_roaster", "items": [{"roaster_id": "good.example"}]},
    )
    assert r.status_code == 200 and r.json()["excluded"] == 2  # the served release's two coffees of the store
    d = post(c, "/api/drafts", {"key": "k2"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    assert {(x["subject"], x["action"]) for x in doc["decisions"]} == {("c1", "exclude"), ("c2", "exclude")}
    assert decisions.not_roasters(doc) == ["good.example"]
    u = post(c, f"/api/sources/flags/{r.json()['batch_id']}/undo", {"key": "k3"})
    assert u.json()["restored"] == 2  # Undo restores the flag and the coffees
    again = post(c, "/api/sources/flags", {"key": "k4", "flag": "not_roaster", "items": [
        {"roaster_id": "good.example", "seen": u_batch(app)}]})  # fmt: skip
    back = post(c, "/api/sources/flags", {"key": "k5", "flag": "roaster", "items": [
        {"roaster_id": "good.example", "seen": again.json()["batch_id"]}]})  # fmt: skip
    assert back.json()["restored"] == 2  # flagged Roaster again: its coffees come back too
    redo = post(c, f"/api/sources/flags/{back.json()['batch_id']}/undo", {"key": "k6"})
    assert redo.json()["excluded"] == 2  # undoing that puts Not a roaster back, and its coffees leave again
    gone = post(c, "/api/sources/flags", {"key": "k7", "flag": "unreviewed", "items": [
        {"roaster_id": "good.example", "seen": u_batch(app)}]})  # fmt: skip
    assert gone.json()["restored"] == 2  # and a later change still finds them


def u_batch(app):
    return app.state.writes.control.source_flags(["good.example"])["good.example"]["batch_id"]


def test_pull_now_is_one_job_and_after_the_merge_crawls_into_next_week(tmp_path, lake, pg_url):
    """Pull now is one Cloud Run job: the probe, then a counted crawl of each storefront found (coffeedata pull).
    No page load starts anything. Once this week's merge is done, it crawls into next week, which the next merge and
    build keep (a merged week's partition is never read again)."""
    from coffeedata import control

    app, c, fake = make(tmp_path, lake.root, pg_url)
    post(
        c, "/api/sources/flags", {"key": "k1", "flag": "roaster", "items": [{"roaster_id": "beans.example"}]}
    )
    plan = post(c, "/api/jobs/pull/plan", {"key": "p0", "roasters": "beans.example,tea.example"}).json()
    assert plan["refusals"] == ["tea.example isn't flagged Roaster: flag it first"]
    assert post(c, "/api/jobs/pull/runs", {"key": "p1", "roasters": "tea.example"}).status_code == 409
    r = post(c, "/api/jobs/pull/runs", {"key": "p2", "roasters": "beans.example"})
    assert r.status_code == 200, r.text
    (run,) = [x for x in fake.calls if x[0] == "run"]
    assert run[1] == "pull" and "coffeedata pull --only beans.example --run-date 2026-11-02" in run[2][0]
    fake.finish("pull")
    for page in ("/part/sources", "/part/jobs", "/part/release"):  # GETs only read
        c.get(page)
    assert len([x for x in fake.calls if x[0] == "run"]) == 1
    lk = Lake(str(lake.root))
    control.finish(lk, control.start(lk, "merge", "2026-11-02"), "done")  # this week is merged
    later = post(c, "/api/jobs/pull/plan", {"key": "p3", "roasters": "beans.example"}).json()
    assert later["refusals"] == [] and later["params"]["week"] == "2026-11-09"
    assert "--run-date 2026-11-09" in later["steps"][0]["args"][0]


def test_a_header_sorts_by_its_column_both_ways(tmp_path, lake, pg_url):
    _, c, _ = make(tmp_path, lake.root, pg_url)
    up = c.get("/part/sources?sort=name&dir=asc").text
    assert up.index("Beans &amp; Roast") < up.index("Tea Corner") and 'aria-sort="ascending"' in up
    down = c.get("/part/sources?sort=name&dir=desc").text
    assert down.index("Tea Corner") < down.index("Beans &amp; Roast") and 'aria-sort="descending"' in down
    assert down.count("aria-sort=") == 1  # only the sorted column says so
    assert "sort=name&amp;dir=asc" in down  # its header flips the direction
    assert "Filter / Sort (1)" in down  # a non-default order counts
    newest = c.get("/part/sources?sort=newest").text  # the phone sheet's option
    assert 'aria-sort="descending"' in newest and 'value="newest" checked' in newest
