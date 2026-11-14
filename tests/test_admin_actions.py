"""The admin's writes (admin/writes.py, admin/jobs.py, admin/controlplane.py) against a real local Postgres
(tests/pg.py) and a fake Cloud Run Jobs API (tests/admin_fakes.py): job starts with their guards, cancel, decisions
and undo, drafts over a snapshot, Publish's readiness, Roll back, and that every write is CSRF-checked, signed in,
idempotent, audited, and fails closed. The lake is never written."""

import json
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from admin import jobs
from admin.config import Settings
from admin.controlplane import ControlPlane, url_connect
from admin.main import create_app
from admin.store import FileStore
from coffeedata import control, decisions
from coffeedata.lake import Lake

from .admin_fakes import FakeJobs
from .test_admin import B1, B2, ORIGIN, SECRET, TOKEN, W1, W2, W3, WRITE, build_lake, tree

THURSDAY = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)  # W3's week, outside both windows
MONDAY = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # inside W3's crawl window


@pytest.fixture
def lake(tmp_path):
    return build_lake(tmp_path / "lake")


def make(tmp_path, lake_root, pg_url, now=THURSDAY, fake=None, control_plane=None):
    fake = fake or FakeJobs()
    st = Settings(origin=ORIGIN, session_secret=SECRET, enroll_token=TOKEN, lake_url=str(lake_root),
                  cache_dir=tmp_path / "cache", store=FileStore(tmp_path / "state"),
                  control=control_plane or ControlPlane(url_connect(pg_url)), jobs=fake,
                  snapshots=FileStore(tmp_path / "snapshots")).check()  # fmt: skip
    app = create_app(st, now=lambda: now)
    c = TestClient(app, base_url=ORIGIN)
    c.cookies.set("admin_session", app.state.auth.start_session("cred-abcdefgh"))
    return app, c, fake


def post(c, url, body):
    return c.post(url, json=body, headers=WRITE)


def audit(app):
    rows, _ = app.state.writes.control.audit_log(500)
    return rows


# --- the registry ----------------------------------------------------------------------------------------------


def test_every_action_names_a_real_pipeline_job_and_checked_fields():
    """One table drives the Jobs page, the API, the guards and the audit: every step is a job in JOB_NAMES (the
    jobs admin.tf lets the admin start, and nothing else), and every field is in FIELDS."""
    for a in jobs.ACTIONS.values():
        assert set(a.fields) <= set(jobs.FIELDS), a.key
    p = {"connectors": "all", "connector": "shopify", "roaster": "onyxcoffeelab.com", "mode": "test",
         "week": W3, "test_id": "t-1", "draft_id": "d-1", "snapshot_id": "s-1", "sha256": "0" * 64,
         "build": B1, "baseline": B1, "reason": "why"}  # fmt: skip
    for a in jobs.ACTIONS.values():
        for s in a.steps(p):
            assert s.job in jobs.JOB_NAMES, (a.key, s.job)
    full = jobs.ACTIONS["crawl"].steps(p)
    assert [s.job for s in full] == sorted(jobs.CRAWL_JOB.values())  # a full crawl: every connector's job
    assert full[0].args[-2:] == ["--test", "t-1"]
    count = jobs.ACTIONS["crawl-one"].steps(p | {"mode": "count"})[0]
    assert count.args == ["crawl", "--connector", "shopify", "--only", "onyxcoffeelab.com", "--run-date", W3]


@pytest.mark.parametrize(
    "raw",
    [{"connector": "shopify; rm -rf /", "roaster": "a.com"}, {"connector": "shopify", "roaster": "a.com --x"},
     {"connector": "shopify", "roaster": ""}, {"connector": "shopify", "roaster": "a.com", "mode": "both"},
     {"connector": "shopify", "roaster": "a.com", "week": "next tuesday"}],
)  # fmt: skip
def test_fields_are_checked_before_anything_starts(raw):
    with pytest.raises(ValueError):
        jobs.checked(jobs.ACTIONS["crawl-one"], raw, date(2026, 11, 5), lambda: "t-1")


def test_the_windows():
    merged = jobs.WeekState(W3, merge_done=True)
    open_ = jobs.WeekState(W3, merge_done=False)
    assert jobs.in_window(MONDAY, open_) and not jobs.in_window(MONDAY, merged)
    assert not jobs.in_window(datetime(2026, 11, 2, 8, 59, tzinfo=UTC), open_)
    assert "Tuesday" in jobs.in_window(datetime(2026, 11, 3, 12, 59, tzinfo=UTC).replace(hour=11), merged)
    assert not jobs.in_window(THURSDAY, open_)


# --- the control plane -----------------------------------------------------------------------------------------


def test_migrations_apply_once_and_every_table_is_append_only(pg_url):
    cp = ControlPlane(url_connect(pg_url))
    assert cp.migrate() == ["0001_control_plane.sql", "0002_evaluations.sql", "0003_rules.sql"]
    assert cp.migrate() == []  # every instance runs it at start: idempotent
    cp.audit_now("owner", "decide", "x", "ok")
    for sql in ("update control.audit set actor = 'x'", "delete from control.audit", "truncate control.audit",
                "delete from control.decisions", "delete from control.rule_settings",
                "update control.roaster_state set crawl = true"):  # fmt: skip
        with pytest.raises(Exception, match="append-only"), cp.tx() as cur:
            cur.execute(sql)
    assert len(cp.audit_log()[0]) == 1


# --- writes: off, signed out, CSRF -----------------------------------------------------------------------------


def test_writes_are_off_without_their_settings_and_reads_still_work(tmp_path, lake):
    st = Settings(origin=ORIGIN, session_secret=SECRET, enroll_token=TOKEN, lake_url=str(lake.root),
                  cache_dir=tmp_path / "cache", store=FileStore(tmp_path / "state")).check()  # fmt: skip
    assert len(st.writes_off) == 3
    app = create_app(st)
    c = TestClient(app, base_url=ORIGIN)
    c.cookies.set("admin_session", app.state.auth.start_session("c"))
    r = post(c, "/api/decisions", {"key": "k"})
    assert r.status_code == 503 and "Writes are off" in r.json()["detail"]
    assert c.get("/part/week").status_code == 200
    jobs_page = c.get("/part/jobs").text
    assert "Actions are off" in jobs_page


def test_every_write_is_refused_signed_out_and_cross_site(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url)
    writes = [
        r for r in app.routes if "POST" in getattr(r, "methods", set()) and not r.path.startswith("/api/auth")
    ]
    # plan, start, cancel, decide, undo, draft, publish, discard, roll back, evaluation, settings check and save, crawl
    assert len(writes) == 13
    anon = TestClient(app, base_url=ORIGIN)
    for r in writes:
        url = r.path.replace("{action}", "merge").replace("{run_id}", "r").replace("{target}", "d")
        url = url.replace("{draft_id}", "d").replace("{build_id}", B1).replace("{roaster_id}", "a.example")
        assert anon.post(url, json={"key": "k"}, headers=WRITE).status_code == 401, url
        assert c.post(url, json={"key": "k"}).status_code == 403, url  # no same-origin markers: CSRF
        assert (
            c.post(url, json={"key": "k"}, headers=WRITE | {"Sec-Fetch-Site": "cross-site"}).status_code
            == 403
        )
    assert not [x for x in fake.calls if x[0] in ("run", "cancel")]


# --- jobs ------------------------------------------------------------------------------------------------------


def test_a_job_start_shows_its_plan_then_starts_once_and_is_audited(tmp_path, lake, pg_url):
    before = tree(lake.root)
    app, c, fake = make(tmp_path, lake.root, pg_url)
    body = {"key": "k1", "connector": "shopify", "roaster": "onyxcoffeelab.com", "mode": "test"}
    plan = post(c, "/api/jobs/crawl-one/plan", body).json()
    assert plan["refusals"] == [] and plan["window"] is None
    (step,) = plan["steps"]
    assert step["job"] == "crawl-shopify" and step["args"][:5] == ["crawl", "--connector", "shopify", "--only",
                                                                  "onyxcoffeelab.com"]  # fmt: skip
    assert step["args"][5] == "--test"
    assert not [x for x in fake.calls if x[0] == "run"]  # the plan starts nothing
    r1 = post(c, "/api/jobs/crawl-one/runs", body)
    r2 = post(c, "/api/jobs/crawl-one/runs", body)  # a double submit: the same key
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    assert len([x for x in fake.calls if x[0] == "run"]) == 1
    (run,) = app.state.writes.control.runs()
    assert (
        run["job"] == "crawl-shopify"
        and run["mode"] == "test"
        and run["actor"].startswith("owner (passkey cred-abc")
    )
    (a,) = [x for x in audit(app) if x["action"] == "job_start"]
    assert a["outcome"] == "ok" and a["detail"]["runs"][0]["execution"].endswith("crawl-shopify-00001")
    # the Jobs page shows it running, with its logs link, and polls while it runs
    page = c.get("/part/jobs").text
    assert "data-poll" in page and "console.cloud.google.com" in page and "running" in page
    # once a day per roaster
    again = post(c, "/api/jobs/crawl-one/runs", body | {"key": "k2"})
    assert again.status_code == 409 and any("in the last day" in x for x in again.json()["reasons"])
    # the job is running: a second start of it is refused (no overlap) and nothing more starts
    merge = post(c, "/api/jobs/crawl/runs", {"key": "k3", "connectors": "shopify", "mode": "test"})
    assert merge.status_code == 409 and any("already running" in x for x in merge.json()["reasons"])
    # finished: success recorded once, and the start is allowed again
    fake.finish("crawl-shopify")
    page = c.get("/part/jobs").text
    assert "succeeded" in page and "data-poll" not in page
    assert app.state.writes.control.runs()[0]["final"] == "succeeded"
    assert [x["outcome"] for x in audit(app) if x["action"] == "job_start"].count("refused") == 2
    assert tree(lake.root) == before  # the admin never writes the lake


def test_the_scheduled_window_refuses_unless_overridden_with_a_reason(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url, now=MONDAY)
    body = {"key": "k1", "week": W3}
    plan = post(c, "/api/jobs/merge/plan", body).json()
    assert "Monday crawl window" in plan["window"]
    r = post(c, "/api/jobs/merge/runs", body)
    assert r.status_code == 409 and r.json()["overridable"] and fake.calls[-1][0] != "run"
    assert post(c, "/api/jobs/merge/runs", body | {"override": "no"}).status_code == 400  # too short
    ok = post(c, "/api/jobs/merge/runs", body | {"override": "the merge failed on a 429; rerun"})
    assert ok.status_code == 200
    assert app.state.writes.control.runs()[0]["override_reason"] == "the merge failed on a 429; rerun"
    assert fake.calls[-1] == ("run", "merge", ["merge", "--run-date", W3])


def test_count_mode_never_crawls_a_merged_week(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url)
    r = post(c, "/api/jobs/crawl/runs", {"key": "k1", "connectors": "generic", "mode": "count", "week": W2})
    assert r.status_code == 409 and any("is merged" in x for x in r.json()["reasons"])
    ok = post(c, "/api/jobs/crawl/runs", {"key": "k2", "connectors": "generic", "mode": "count", "week": W3})
    assert ok.status_code == 200 and fake.calls[-1] == ("run", "crawl-generic",
                                                         ["crawl", "--connector", "generic", "--run-date", W3])  # fmt: skip


def test_a_full_crawl_records_what_started_when_cloud_run_refuses_one(tmp_path, lake, pg_url):
    fake = FakeJobs()
    fake.refuse = {"crawl-generic"}
    app, c, _ = make(tmp_path, lake.root, pg_url, fake=fake)
    r = post(c, "/api/jobs/crawl/runs", {"key": "k1", "connectors": "all", "mode": "test"})
    out = r.json()
    assert (
        r.status_code == 200
        and not out["ok"]
        and len(out["runs"]) == 4
        and "crawl-generic" in out["errors"][0]
    )
    assert len(app.state.writes.control.runs()) == 4
    (a,) = [x for x in audit(app) if x["action"] == "job_start"]
    assert a["outcome"] == "failed" and len(a["detail"]["runs"]) == 4


def test_a_job_is_never_started_when_the_control_plane_cant_record_it(tmp_path, lake, pg_url):
    dead = ControlPlane(url_connect("postgresql://nobody@127.0.0.1:1/none"))
    app, c, fake = make(tmp_path, lake.root, pg_url, control_plane=dead)
    r = post(c, "/api/jobs/merge/runs", {"key": "k1", "week": W3})
    assert r.status_code == 503 and "nothing was done" in r.json()["detail"]
    assert not [x for x in fake.calls if x[0] == "run"]
    page = c.get("/part/jobs")  # the page degrades: no runs or actions, and it says why
    assert page.status_code == 200 and "be read right now" in page.text and "data-write=" not in page.text


def test_cloud_run_down_starts_nothing_and_says_so(tmp_path, lake, pg_url):
    fake = FakeJobs()
    app, c, _ = make(tmp_path, lake.root, pg_url, fake=fake)
    fake.down = True
    r = post(c, "/api/jobs/merge/runs", {"key": "k1", "week": W3})
    assert r.status_code == 409 and "Cloud Run" in r.json()["reasons"][0]
    assert app.state.writes.control.runs() == []


def test_cancel_a_running_job(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url)
    post(c, "/api/jobs/fetch-health/runs", {"key": "k1"})
    (run,) = app.state.writes.control.runs()
    assert post(c, f"/api/jobs/runs/{run['run_id']}/cancel", {"key": "k2"}).status_code == 200
    assert fake.calls[-1][0] == "cancel" and "cancelled" in c.get("/part/jobs").text
    assert post(c, "/api/jobs/runs/nope/cancel", {"key": "k3"}).status_code == 400


# --- decisions -------------------------------------------------------------------------------------------------


def _item(cid, ih=None, seen_c=None, seen_h=None):
    return {"coffee_id": cid, "input_hash": ih, "roaster_id": "good.example", "title": cid,
            "seen_coffee_id": seen_c, "seen_input_hash": seen_h}  # fmt: skip


def test_decide_undo_a_stale_tab_and_the_audit(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    d = post(c, "/api/decisions", {"key": "k1", "action": "exclude", "kind": "coffee_id", "reason": "a drink mix",
                                   "items": [_item("c1")], "seen": {"c1": None}}).json()  # fmt: skip
    assert d["count"] == 1 and d["group"] is None
    # a second tab that loaded before it can't overwrite it
    stale = post(c, "/api/decisions", {"key": "k2", "action": "include", "kind": "coffee_id", "reason": "coffee",
                                       "items": [_item("c1")], "seen": {"c1": None}})  # fmt: skip
    assert stale.status_code == 409 and "changed since" in stale.json()["detail"]
    assert (
        post(
            c,
            "/api/decisions",
            {
                "key": "k3",
                "action": "exclude",
                "kind": "coffee_id",
                "reason": "",
                "items": [_item("c2")],
                "seen": {},
            },
        ).status_code
        == 400
    )  # a reason is required
    page = c.get(f"/part/catalog?build={B2}").text
    assert "excluded" in page and f"/api/decisions/{d['decisions'][0]}/undo" in page
    eff = app.state.writes.control.effective()
    assert [(e["subject"], e["action"]) for e in eff] == [("c1", "exclude")]
    u = post(c, f"/api/decisions/{d['decisions'][0]}/undo", {"key": "k4", "reason": "changed my mind"}).json()
    assert u["count"] == 1 and app.state.writes.control.effective() == []
    assert (
        post(c, f"/api/decisions/{d['decisions'][0]}/undo", {"key": "k5"}).json()["count"] == 0
    )  # superseded
    acts = [x["action"] for x in audit(app)]
    assert acts.count("decide") == 1 and acts.count("undo") == 2
    log = c.get("/part/log?action=decide").text
    assert "a drink mix" in log and "1 decide recorded" in log


def test_a_group_is_decided_and_undone_together(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    items = [_item("c1", "h1"), _item("c2", "h2"), _item("m1", "h3")]
    d = post(c, "/api/decisions", {"key": "k1", "action": "include", "kind": "input_hash", "reason": "same pasted body",
                                   "items": items, "seen": {}}).json()  # fmt: skip
    assert d["count"] == 3 and d["group"]
    assert {e["subject_kind"] for e in app.state.writes.control.effective()} == {"input_hash"}
    assert post(c, f"/api/decisions/{d['group']}/undo", {"key": "k2"}).json()["count"] == 3
    assert app.state.writes.control.effective() == []


# --- release: draft, publish, roll back ------------------------------------------------------------------------


def _draft_build(lake_root, draft_id, snapshot_doc, passed=True, built=None):
    """What the build job records for a draft (cli._build): its builds rows with kind draft and snapshot, and its
    release_changes. Written here with a writable lake, as the job would."""
    lake = Lake(str(lake_root))
    b3 = "20261105T120000-cccccc"
    base = {"build_id": b3, "built_at": built or datetime(2026, 11, 5, 12, 30, tzinfo=UTC),
            "run_date": date.fromisoformat(W3)}  # fmt: skip
    lake.write("builds", f"builds/run_date={W3}/build={b3}.parquet", [
        base | {"kind": "build", "unique_id": "build", "status": "pass" if passed else "fail"},
        base | {"kind": "draft", "unique_id": draft_id},
        base | {"kind": "snapshot", "unique_id": snapshot_doc["snapshot_id"], "checksum": decisions.digest(snapshot_doc),
                "payload": json.dumps(snapshot_doc, sort_keys=True)},
    ])  # fmt: skip
    import pyarrow as pa

    from coffeedata.lake import SCHEMAS

    rows = [{"build_id": b3, "table_name": "public_coffee", "item": "c1", "change": "removed",
             "reason": "decision:exclude"}]  # fmt: skip
    lake.write_ops("release_changes", W3, b3, pa.Table.from_pylist(rows, SCHEMAS["release_changes"]))
    return b3


def test_a_draft_snapshots_the_decisions_then_publish_waits_for_every_check(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url)
    assert post(c, "/api/drafts", {"key": "k0"}).status_code == 400  # nothing to build yet
    post(c, "/api/decisions", {"key": "k1", "action": "exclude", "kind": "coffee_id", "reason": "a drink mix",
                               "items": [_item("c1")], "seen": {}})  # fmt: skip
    r = post(c, "/api/drafts", {"key": "k2"}).json()
    did, sid = r["draft_id"], r["snapshot_id"]
    # the snapshot is in the store, immutable, and the build job got its id and digest
    doc = json.loads((tmp_path / "snapshots" / decisions.key(sid)).read_text())
    sha = decisions.digest(doc)
    assert [d["subject"] for d in doc["decisions"]] == ["c1"]
    run_call = fake.calls[-1]
    assert (
        run_call[1] == "build" and f"--draft {did} --snapshot {sid} --snapshot-sha256 {sha}" in run_call[2][0]
    )
    assert "building" in c.get("/part/release").text
    # The build lands (the fake job finishes; the build's rows are as cli._build writes them).
    fake.finish("build")
    b3 = _draft_build(lake.root, did, doc)
    page = c.get(f"/part/release?build={b3}").text
    assert "Live check" in page and "good.example" in page and 'aria-disabled="true"' in page
    refused = post(c, f"/api/drafts/{did}/publish", {"key": "k3", "reason": "go ahead"})
    assert refused.status_code == 409 and any("Live check" in x for x in refused.json()["reasons"])
    # The live check: a test crawl of the store that answered (its job_manifests end row says the test id).
    post(c, "/api/jobs/crawl-one/runs", {"key": "k4", "connector": "shopify", "roaster": "good.example"})
    test_id = app.state.writes.control.runs()[0]["test_id"]
    w = Lake(str(lake.root))
    job = control.start(w, "crawl", W3, "shopify", 1, detail={"test": test_id, "arm": "test"})
    control.finish(w, job, "done", ok=1, errors=0, detail={"test": test_id, "arm": "test"})
    fake.finish()
    assert "succeeded" in c.get("/part/jobs").text and "store: answered" in c.get("/part/jobs").text
    ok = post(c, f"/api/drafts/{did}/publish", {"key": "k5", "reason": "publish the drink-mix removal"})
    assert ok.status_code == 200, ok.json()
    assert fake.calls[-1] == ("run", "publish", ["publish-release", "--build", b3, "--action", "publish",
                                                 "--baseline", B1, "--reason=publish the drink-mix removal"])  # fmt: skip
    assert "publishing" in c.get(f"/part/release?build={b3}").text
    assert any(x["action"] == "job_start" and x["subject"] == "publish" for x in audit(app))


def test_publish_asks_for_the_count_when_more_than_50_rows_leave(tmp_path, lake, pg_url, monkeypatch):
    from admin import writes as w

    monkeypatch.setattr(w, "TYPE_OVER", 0)  # one row leaves in this lake: over the (lowered) bar
    monkeypatch.setattr(w, "LIVE_CHECK_DAYS", 0)
    app, c, fake = make(tmp_path, lake.root, pg_url)
    post(c, "/api/decisions", {"key": "k1", "action": "include", "kind": "coffee_id", "reason": "it is coffee",
                               "items": [_item("m1")], "seen": {}})  # fmt: skip
    r = post(c, "/api/drafts", {"key": "k2"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(r["snapshot_id"])).read_text())
    fake.finish()
    b3 = _draft_build(lake.root, r["draft_id"], doc)
    page = c.get(f"/part/release?build={b3}").text
    assert 'data-type="1"' in page and "data-danger" in page
    no = post(c, f"/api/drafts/{r['draft_id']}/publish", {"key": "k3", "reason": "go ahead", "typed": "2"})
    assert no.status_code == 409 and any("type that number" in x for x in no.json()["reasons"])
    yes = post(c, f"/api/drafts/{r['draft_id']}/publish", {"key": "k4", "reason": "go ahead", "typed": "1"})
    assert yes.status_code == 200


def test_a_stale_or_discarded_draft_never_publishes(tmp_path, lake, pg_url, monkeypatch):
    from admin import writes as w

    monkeypatch.setattr(w, "LIVE_CHECK_DAYS", 0)
    app, c, fake = make(tmp_path, lake.root, pg_url)
    post(c, "/api/decisions", {"key": "k1", "action": "include", "kind": "coffee_id", "reason": "it is coffee",
                               "items": [_item("m1")], "seen": {}})  # fmt: skip
    r = post(c, "/api/drafts", {"key": "k2"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(r["snapshot_id"])).read_text())
    fake.finish()
    _draft_build(lake.root, r["draft_id"], doc)
    # another release was served since: the draft is stale
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.Table.from_pylist([{"build_id": B2, "run_date": date.fromisoformat(W2), "reason": "x",
                                          "served_at": datetime(2026, 11, 4, tzinfo=UTC)}]),
                   lake.root / "marts" / "release.parquet")  # fmt: skip
    assert "stale" in c.get("/part/release").text
    st = post(c, f"/api/drafts/{r['draft_id']}/publish", {"key": "k3", "reason": "go ahead"})
    assert st.status_code == 409 and any("baseline" in x for x in st.json()["reasons"])
    assert post(c, f"/api/drafts/{r['draft_id']}/discard", {"key": "k4"}).status_code == 200
    assert "discarded" in c.get("/part/release").text
    gone = post(c, f"/api/drafts/{r['draft_id']}/publish", {"key": "k5", "reason": "go ahead"})
    assert gone.status_code == 409 and "discarded" in gone.json()["reasons"][0]


def test_roll_back_only_to_a_release_that_was_served(tmp_path, lake, pg_url):
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.Table.from_pylist([{"build_id": "20261103T000000-dddddd", "run_date": date.fromisoformat(W2),
                                          "reason": "x", "served_at": datetime(2026, 11, 4, tzinfo=UTC)}]),
                   lake.root / "marts" / "release.parquet")  # fmt: skip
    app, c, fake = make(tmp_path, lake.root, pg_url)
    assert (
        post(c, f"/api/releases/{B2}/rollback", {"key": "k1", "reason": "bad"}).status_code == 409
    )  # failed
    r = post(c, f"/api/releases/{B1}/rollback", {"key": "k2", "reason": "the publish removed too much"})
    assert r.status_code == 200
    assert fake.calls[-1] == ("run", "publish", ["publish-release", "--build", B1, "--action", "rollback",
                                                 "--reason=the publish removed too much"])  # fmt: skip


def test_the_decisions_export_feeds_export_labels(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    post(c, "/api/decisions", {"key": "k1", "action": "exclude", "kind": "coffee_id", "reason": "a drink mix",
                               "items": [_item("c1")], "seen": {}})  # fmt: skip
    r = c.get("/api/decisions/export")
    doc = r.json()
    assert "attachment" in r.headers["content-disposition"] and decisions.check(doc)[0]["subject"] == "c1"
    rows, skipped = decisions.export_labels(doc, set())
    assert rows[0]["label"] == "not_coffee" and rows[0]["set"] == "train"
    assert W1  # the lake's first week stays untouched


# --- the Cloud Run client and the settings ---------------------------------------------------------------------


def test_the_cloud_run_client_sends_overrides_and_reads_executions():
    import httpx
    import respx

    from admin.runjobs import API, CloudRunJobs, JobsError

    parent = "projects/coffee-test-1/locations/us-central1"
    ex = {"name": f"{parent}/jobs/merge/executions/merge-abc12", "job": f"{parent}/jobs/merge", "taskCount": 1,
          "createTime": "2026-11-05T12:00:00Z", "startTime": "2026-11-05T12:00:05Z",
          "logUri": "https://console.cloud.google.com/logs/viewer?x=1"}  # fmt: skip
    with respx.mock(assert_all_called=True) as m:
        m.get(url__startswith="http://metadata.google.internal/").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))  # fmt: skip
        run = m.post(f"{API}/{parent}/jobs/merge:run").mock(return_value=httpx.Response(200, json={
            "name": f"{parent}/operations/op1", "metadata": ex}))  # fmt: skip
        m.get(f"{API}/{parent}/jobs/merge/executions").mock(return_value=httpx.Response(200, json={"executions": [
            ex | {"completionTime": "2026-11-05T12:10:05Z", "succeededCount": 1},
            ex | {"name": f"{parent}/jobs/merge/executions/merge-zzz", "completionTime": "2026-11-05T12:10:05Z",
                  "failedCount": 1, "conditions": [{"type": "Completed", "message": "Task failed: exit 1"}],
                  "logUri": "javascript:alert(1)"}]}))  # fmt: skip
        cancel = m.post(f"{API}/{ex['name']}:cancel").mock(return_value=httpx.Response(200, json={}))
        c = CloudRunJobs("coffee-test-1", "us-central1")
        e = c.run("merge", ["merge", "--run-date", W3])
        assert e.status == "running" and e.short == "merge-abc12" and e.log_uri
        sent = json.loads(run.calls[0].request.content)
        assert sent == {"overrides": {"containerOverrides": [{"args": ["merge", "--run-date", W3]}]}}
        assert run.calls[0].request.headers["authorization"] == "Bearer tok"
        done, failed = c.executions("merge")
        assert (done.status, done.duration_s) == ("succeeded", 600.0)
        assert (
            failed.status == "failed" and failed.message == "Task failed: exit 1" and failed.log_uri is None
        )
        c.cancel(ex["name"])
        assert cancel.called
    with pytest.raises(ValueError):
        c.cancel(
            "projects/other-project/locations/us-central1/jobs/merge/executions/x"
        )  # only its own project
    with pytest.raises(ValueError):
        c.run("../jobs", None)
    with respx.mock() as m:
        m.get(url__startswith="http://metadata.google.internal/").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))  # fmt: skip
        m.post(f"{API}/{parent}/jobs/merge:run").mock(return_value=httpx.Response(403, json={
            "error": {"message": "Permission denied"}}))  # fmt: skip
        with pytest.raises(JobsError, match="403"):
            CloudRunJobs("coffee-test-1", "us-central1").run("merge", None)


def test_hosted_writes_need_cloud_sql_and_the_snapshot_bucket(monkeypatch):
    from admin.config import from_env

    for k in ("ADMIN_DB_INSTANCE", "ADMIN_SNAPSHOT_BUCKET", "ADMIN_SNAPSHOT_DIR", "ADMIN_RUN_REGION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("K_SERVICE", "registry-coffee-admin")
    monkeypatch.setenv("ADMIN_ORIGIN", "https://admin.registry.coffee")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_STATE_BUCKET", "coffee-admin-state")
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql://u@db.example/admin")
    monkeypatch.setenv("ADMIN_SNAPSHOT_DIR", "/tmp/snaps")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "coffee-test-1")
    monkeypatch.setenv("ADMIN_RUN_REGION", "us-central1")
    st = from_env()
    assert not st.problems  # sign-in and the pages work
    assert st.control is None and any("for a laptop" in w for w in st.writes_off)
    assert any("ADMIN_SNAPSHOT_BUCKET" in w for w in st.writes_off)
    monkeypatch.delenv("ADMIN_DATABASE_URL")
    monkeypatch.delenv("ADMIN_SNAPSHOT_DIR")
    monkeypatch.setenv("ADMIN_DB_INSTANCE", "coffee-test-1:us-central1:registry-coffee-admin")
    monkeypatch.setenv("ADMIN_DB_USER", "registry-coffee-admin-run@coffee-test-1.iam")
    monkeypatch.setenv("ADMIN_SNAPSHOT_BUCKET", "coffee-test-1-admin-snapshots")
    st = from_env()  # the Connector is made, never connected, until the first write
    assert st.writes_off == [] and st.control is not None


# --- the review's fixes ----------------------------------------------------------------------------------------


def test_roll_back_inside_a_window_with_a_recorded_reason(tmp_path, lake, pg_url):
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.Table.from_pylist([{"build_id": "20261103T000000-dddddd", "run_date": date.fromisoformat(W2),
                                          "reason": "x", "served_at": datetime(2026, 11, 4, tzinfo=UTC)}]),
                   lake.root / "marts" / "release.parquet")  # fmt: skip
    tuesday = datetime(2026, 11, 3, 6, 30, tzinfo=UTC)
    app, c, fake = make(tmp_path, lake.root, pg_url, now=tuesday)
    r = post(c, f"/api/releases/{B1}/rollback", {"key": "k1", "reason": "bad release"})
    assert r.status_code == 409 and r.json()["overridable"]
    ok = post(c, f"/api/releases/{B1}/rollback", {"key": "k2", "reason": "bad release",
                                                 "override": "the release went out with wrong prices"})  # fmt: skip
    assert ok.status_code == 200 and fake.calls[-1][1] == "publish"


def test_a_refused_draft_writes_no_snapshot(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url, now=MONDAY)
    post(c, "/api/decisions", {"key": "k1", "action": "exclude", "kind": "coffee_id", "reason": "a drink mix",
                               "items": [_item("c1")], "seen": {}})  # fmt: skip
    assert post(c, "/api/drafts", {"key": "k2"}).status_code == 409
    assert not (tmp_path / "snapshots").exists() or not list((tmp_path / "snapshots").rglob("*.json"))
    assert (
        post(c, "/api/drafts", {"key": "k3", "override": "decided on Monday, build now"}).status_code == 200
    )


def test_two_tabs_cant_both_start_the_same_job(tmp_path, lake, pg_url):
    """The plan's overlap check passed for both, but the start checks again under the job's lock."""

    class Racing(FakeJobs):
        def executions(
            self, job, limit=10
        ):  # the plan sees nothing running; the start sees the other tab's run
            self.calls.append(("list", job))
            listed = [x for x in self.calls if x == ("list", job)]
            return [] if len(listed) == 1 else super().executions(job, limit)

    fake = Racing()
    fake.run("merge", ["merge"])  # the other tab's start landed in between
    app, c, _ = make(tmp_path, lake.root, pg_url, fake=fake)
    r = post(c, "/api/jobs/merge/runs", {"key": "k1", "week": W3})
    assert r.status_code == 409 and "already running" in r.json()["reasons"][0]
    assert [x for x in fake.calls if x[0] == "run"] == [("run", "merge", ["merge"])]
    assert app.state.writes.control.runs() == []


def test_a_start_that_cant_be_recorded_is_recorded_again_or_named(tmp_path, lake, pg_url, monkeypatch):
    from admin.controlplane import ControlPlaneError

    app, c, fake = make(tmp_path, lake.root, pg_url)
    cp = app.state.writes.control
    real = cp.record_run
    fails = {"n": 1}

    def flaky(cur, run):
        if fails["n"]:
            fails["n"] -= 1
            raise ControlPlaneError("connection dropped")
        return real(cur, run)

    monkeypatch.setattr(cp, "record_run", flaky)
    r = post(c, "/api/jobs/merge/runs", {"key": "k1", "week": W3})
    assert r.status_code == 200 and len(cp.runs()) == 1  # written again in a new transaction
    fails["n"] = 2
    fake.finish()
    r = post(c, "/api/jobs/merge/runs", {"key": "k2", "week": W3})
    assert r.status_code == 500 and "couldn't record it" in r.json()["detail"]
    again = post(c, "/api/jobs/merge/runs", {"key": "k2", "week": W3})  # a retry starts nothing twice
    assert again.status_code == 409 and len([x for x in fake.calls if x[0] == "run"]) == 2


def test_the_live_check_covers_only_what_the_draft_removes_and_step_9_counts(
    tmp_path, lake, pg_url, monkeypatch
):
    """An exclude already served needs no new live check; Publish's evaluation check counts the public coffees the
    decisions change and refuses over 50 (CLAUDE.md step 9)."""
    from admin import writes as w

    served = decisions.snapshot("s-served", "t", [dict(_item("c1"), subject_kind="coffee_id", subject="c1",
                                                       action="exclude", decision_id="d0")])  # fmt: skip
    Lake(str(lake.root)).write("builds", f"builds/run_date={W1}/build={B1}-snap.parquet", [{
        "build_id": B1, "built_at": datetime(2026, 10, 19, 20, tzinfo=UTC), "run_date": date.fromisoformat(W1),
        "kind": "snapshot", "unique_id": "s-served", "checksum": decisions.digest(served),
        "payload": json.dumps(served, sort_keys=True)}])  # fmt: skip
    app, c, fake = make(tmp_path, lake.root, pg_url)
    post(c, "/api/decisions", {"key": "k1", "action": "exclude", "kind": "coffee_id", "reason": "a drink mix",
                               "items": [_item("c1")], "seen": {}})  # fmt: skip
    post(c, "/api/decisions", {"key": "k2", "action": "include", "kind": "coffee_id", "reason": "it is coffee",
                               "items": [_item("m1")], "seen": {}})  # fmt: skip
    r = post(c, "/api/drafts", {"key": "k3"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(r["snapshot_id"])).read_text())
    fake.finish()
    b3 = _draft_build(lake.root, r["draft_id"], doc)
    page = c.get(f"/part/release?build={b3}").text
    assert "this draft removes nothing" in page  # c1's exclude was served already
    assert "this draft changes 0 public coffees" in page  # c1's exclude is the served release's already
    monkeypatch.setattr(w, "STEP9_LIMIT", -1)
    no = post(c, f"/api/drafts/{r['draft_id']}/publish", {"key": "k4", "reason": "go ahead"})
    assert no.status_code == 409 and any("step 9" in x for x in no.json()["reasons"])


def test_unpublished_drafts_are_not_the_weeks_build_or_a_baseline(tmp_path, lake):
    from admin import queries

    _draft_build(lake.root, "draft-x", decisions.snapshot("s-x", "t", []))
    ro = Lake(str(lake.root), read_only=True)
    assert all(b["build_id"].endswith(("aaaaaa", "bbbbbb")) for b in queries.builds(ro, ro.connect()))
    assert queries.week_build(ro, ro.connect(), W3) is None
    assert len(queries.builds(ro, ro.connect(), drafts=True)) == 3


def test_a_full_test_crawl_keeps_each_connectors_answer(tmp_path, lake):
    from admin import queries

    w = Lake(str(lake.root))
    for conn, ok in (("shopify", 1), ("generic", 0)):
        job = control.start(w, "crawl", W3, conn, 1, detail={"test": "t-9", "arm": "test"})
        control.finish(w, job, "done", ok=ok, errors=1 - ok, detail={"test": "t-9", "arm": "test"})
    ro = Lake(str(lake.root), read_only=True)
    got = {a["connector"]: a["ok"] for a in queries.test_results(ro, ro.connect(), ["t-9"])["t-9"]}
    assert got == {"shopify": 1, "generic": 0}


def test_cloud_run_reads_are_retried_once_and_starts_never():
    import httpx
    import respx

    from admin.runjobs import API, CloudRunJobs, JobsError

    parent = "projects/coffee-test-1/locations/us-central1"
    with respx.mock() as m:
        m.get(url__startswith="http://metadata.google.internal/").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))  # fmt: skip
        listing = m.get(f"{API}/{parent}/jobs/merge/executions").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"executions": []})])  # fmt: skip
        start = m.post(f"{API}/{parent}/jobs/merge:run").mock(return_value=httpx.Response(503))
        c = CloudRunJobs("coffee-test-1", "us-central1")
        assert c.executions("merge") == [] and listing.call_count == 2
        with pytest.raises(JobsError, match="503"):
            c.run("merge", None)
        assert start.call_count == 1


def test_an_idle_connection_is_dropped_and_a_double_submit_answers_once(pg_url, monkeypatch):
    import threading

    from admin import controlplane

    made = []
    connect = url_connect(pg_url)
    cp = ControlPlane(lambda: made.append(1) or connect())
    cp.migrate()
    monkeypatch.setattr(controlplane, "IDLE_S", 0)
    cp.ping()
    assert len(made) == 2  # the pooled one was idle too long: a new connection
    monkeypatch.setattr(controlplane, "IDLE_S", 120)
    out = []
    item = [{"coffee_id": "c1", "input_hash": "h1"}]
    ts = [threading.Thread(target=lambda: out.append(cp.decide("o", "same", "exclude", "coffee_id", "a drink mix",
                                                               item, {"c1": None}))) for _ in range(2)]  # fmt: skip
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(out) == 2 and out[0] == out[1]


def test_close_ends_the_pools_connections(pg_url):
    """close() ends every pooled connection (the tests close each pool at teardown, conftest.pg_url, so a worker
    never runs out of Postgres connections); the next transaction opens a new one."""
    db = pg_url.rsplit("/", 1)[1]
    probe = url_connect(pg_url.rsplit("/", 1)[0] + "/postgres")

    def clients() -> int:
        con = probe()
        try:
            cur = con.cursor()
            cur.execute("select count(*) from pg_stat_activity where datname = %s", (db,))
            return cur.fetchone()[0]
        finally:
            con.close()

    cp = ControlPlane(url_connect(pg_url))
    cp.migrate()
    assert clients() == 1
    cp.close()
    assert clients() == 0
    cp.ping()
    assert clients() == 1
    cp.close()


# --- the final review's fixes ----------------------------------------------------------------------------------


class _Members:
    """A Catalog stand-in for who a decision covers: `rows` as Catalog.members returns them."""

    def __init__(self, rows):
        self.rows = rows

    def members(self, build_id, coffee_ids, hashes):
        return [r for r in self.rows if r["coffee_id"] in coffee_ids or r["input_hash"] in hashes]


def _draft_with(c, tmp_path, lake, fake, items, action="exclude", kind="coffee_id", key="k"):
    post(c, "/api/decisions", {"key": key + "d", "action": action, "kind": kind, "reason": "the owner's call",
                               "items": items})  # fmt: skip
    r = post(c, "/api/drafts", {"key": key + "r"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(r["snapshot_id"])).read_text())
    fake.finish("build")
    return r["draft_id"], _draft_build(lake.root, r["draft_id"], doc)


def test_a_drafts_gate_says_it_passed_against_the_last_published_build(tmp_path, lake, pg_url):
    app, c, fake = make(tmp_path, lake.root, pg_url)
    _, b3 = _draft_with(c, tmp_path, lake, fake, [_item("c1")])
    page = c.get(f"/part/release?build={b3}").text
    assert f"Build {b3} passed" in page and f"compared with {B1}" in page


def test_the_live_check_counts_any_answer_a_connector_test_and_every_roaster_of_a_version(
    tmp_path, lake, pg_url
):
    app, c, fake = make(tmp_path, lake.root, pg_url)
    app.state.writes.catalog = _Members([
        {"coffee_id": "c1", "input_hash": "h1", "roaster_id": "good.example", "platform": "shopify"},
        {"coffee_id": "x9", "input_hash": "h1", "roaster_id": "other.example", "platform": "woocommerce"},
    ])  # fmt: skip
    did, b3 = _draft_with(c, tmp_path, lake, fake, [_item("c1", "h1")], kind="input_hash")
    page = c.get(f"/part/release?build={b3}").text
    assert "good.example" in page and "other.example" in page  # the version's every roaster
    w = Lake(str(lake.root))

    def test(fill, ok):
        post(c, "/api/jobs/" + ("crawl-one" if "roaster" in fill else "crawl") + "/runs",
             {"key": f"t{len(fake.ex)}", "mode": "test"} | fill)  # fmt: skip
        run = app.state.writes.control.runs()[0]
        job = control.start(w, "crawl", W3, run["args"][2], 1, detail={"test": run["test_id"], "arm": "test"})
        control.finish(w, job, "done", ok=ok, errors=1 - ok, detail={"test": run["test_id"], "arm": "test"})
        fake.finish()

    test({"connector": "shopify", "roaster": "good.example"}, 1)  # answered...
    with pytest.MonkeyPatch.context() as m:  # ...then a later failed test of the same store
        m.setattr(app.state.writes.control, "recent_test", lambda *a, **k: None)
        test({"connector": "shopify", "roaster": "good.example"}, 0)
    test({"connectors": "woocommerce"}, 1)  # a whole-connector test covers other.example
    page = c.get(f"/part/release?build={b3}").text
    assert "2 of 2 stores answered" in page


def test_step_9s_running_total_spans_drafts_until_an_evaluation_is_recorded(
    tmp_path, lake, pg_url, monkeypatch
):
    from admin import writes as w

    monkeypatch.setattr(w, "STEP9_LIMIT", 1)
    monkeypatch.setattr(w, "LIVE_CHECK_DAYS", 0)
    app, c, fake = make(tmp_path, lake.root, pg_url)
    cp = app.state.writes.control
    did, b3 = _draft_with(c, tmp_path, lake, fake, [_item("c1")])
    assert "1 in all" in c.get(f"/part/release?build={b3}").text
    # a draft of the same total, published before: its count joins the running total
    monkeypatch.setattr(cp, "step9_total", lambda served: (1, None))
    no = post(c, f"/api/drafts/{did}/publish", {"key": "p1", "reason": "go ahead"})
    assert no.status_code == 409 and any("over 50" in x and "record it" in x for x in no.json()["reasons"])
    assert "Publish fewer" not in json.dumps(no.json())
    monkeypatch.undo()
    monkeypatch.setattr(w, "STEP9_LIMIT", 1)
    monkeypatch.setattr(w, "LIVE_CHECK_DAYS", 0)
    assert post(c, "/api/evaluations", {"key": "e1", "path": "../etc"}).status_code == 400
    assert post(c, "/api/evaluations", {"key": "e2", "path": "eval/classifier-2026-11/"}).status_code == 200
    assert cp.step9_total(set())[1]["path"] == "eval/classifier-2026-11/"


def test_a_publishing_or_published_draft_cant_be_discarded(tmp_path, lake, pg_url, monkeypatch):
    from admin import writes as w

    monkeypatch.setattr(w, "LIVE_CHECK_DAYS", 0)
    app, c, fake = make(tmp_path, lake.root, pg_url)
    did, b3 = _draft_with(c, tmp_path, lake, fake, [_item("m1")], action="include")
    assert post(c, f"/api/drafts/{did}/publish", {"key": "p1", "reason": "go ahead"}).status_code == 200
    r = post(c, f"/api/drafts/{did}/discard", {"key": "x1"})
    assert r.status_code == 409 and "publishing" in r.json()["reasons"][0]


def test_a_cancel_that_cant_be_recorded_is_recorded_again(tmp_path, lake, pg_url, monkeypatch):
    from admin.controlplane import ControlPlaneError

    app, c, fake = make(tmp_path, lake.root, pg_url)
    post(c, "/api/jobs/fetch-health/runs", {"key": "k1"})
    (run,) = app.state.writes.control.runs()
    cp = app.state.writes.control
    real, fails = cp.audit, {"n": 1}

    def flaky(cur, actor, action, *a, **k):
        if action == "job_cancel" and fails["n"]:
            fails["n"] -= 1
            raise ControlPlaneError("connection dropped")
        return real(cur, actor, action, *a, **k)

    monkeypatch.setattr(cp, "audit", flaky)
    assert post(c, f"/api/jobs/runs/{run['run_id']}/cancel", {"key": "k2"}).status_code == 200
    assert [x["action"] for x in audit(app)].count("job_cancel") == 1 and fake.calls[-1][0] == "cancel"


def test_the_catalog_degrades_when_the_control_plane_is_down(tmp_path, lake, pg_url):
    dead = ControlPlane(url_connect("postgresql://nobody@127.0.0.1:1/none"))
    app, c, _ = make(tmp_path, lake.root, pg_url, control_plane=dead)
    page = c.get(f"/part/catalog?build={B2}")
    assert page.status_code == 200 and "of 3 products" in page.text and "be read right now" in page.text
    assert "data-write=" not in page.text
    release = c.get("/part/release")
    assert release.status_code == 200 and "Gate" in release.text


def test_the_export_lists_every_coffee_of_a_version(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    app.state.writes.catalog = _Members([])  # the export reads the served build's own catalog
    import admin.main as m  # noqa: F401 - the route's catalog is the app's; use its members over the real cache

    post(c, "/api/decisions", {"key": "k1", "action": "exclude", "kind": "input_hash", "reason": "a drink mix",
                               "items": [_item("c1", "h-none")]})  # fmt: skip
    doc = c.get("/api/decisions/export").json()
    (d,) = doc["decisions"]
    assert d["subject_kind"] == "input_hash" and d["coffee_ids"] == []  # no coffee in the served build has it
    rows, skipped = decisions.export_labels(doc, set())
    assert rows == [] and skipped == 1  # a version with no listed coffees is never exported
