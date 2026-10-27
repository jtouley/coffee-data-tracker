"""Claude's coffee-or-not verdicts: what gets sent, how answers are stored, and how the build uses them."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from coffeedata import verdict

from .lakehelp import probe, product, run
from .test_dbt import build

RID = "onyxcoffeelab.com"


def _msg(label, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps({"label": label}))]
    )


def test_parse_takes_only_a_known_label():
    assert verdict.parse(_msg("coffee")) == "coffee"
    assert verdict.parse(_msg("maybe")) is None
    assert verdict.parse(_msg("coffee", stop="refusal")) is None
    assert (
        verdict.parse(
            SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="x")])
        )
        is None
    )


def test_request_carries_the_definition_and_the_listing():
    it = verdict.item({"title": "Roasted Sunflower Seeds", "product_type": "", "tags": ["snacks"], "grams": [454],
                       "body_text": "Salted."})  # fmt: skip
    req = verdict.request(it, "claude-haiku-5-5")
    assert "Green (unroasted) coffee" in req["system"][0]["text"]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert (
        "Roasted Sunflower Seeds" in req["messages"][0]["content"] and "454" in req["messages"][0]["content"]
    )
    assert req["output_config"]["format"]["schema"]["properties"]["label"]["enum"] == list(verdict.LABELS)


def _week(lake):
    probe(lake, "2026-10-18", RID)
    run(lake, "2026-10-19", RID, [
        product("A", title="Ethiopia Guji Natural"),
        product("S", title="Roasted Sunflower Seeds"),  # the rules call this coffee ("roast")
        product("G", title="Ethiopia Guji - Unroasted Green Coffee"),  # excluded by rule: never sent
    ])  # fmt: skip


def test_pending_skips_rule_exclusions_answered_and_in_flight_versions(lake, monkeypatch):
    _week(lake)
    todo = verdict.pending(lake, "m", 100)
    assert sorted(r["title"] for r in todo) == ["Ethiopia Guji Natural", "Roasted Sunflower Seeds"]
    by_title = {r["title"]: r["input_hash"] for r in todo}
    at = datetime(2026, 10, 20, tzinfo=UTC)
    lake.write_partition("verdicts", "2026-10-20", "b1", [
        {"decided_at": at, "input_hash": by_title["Ethiopia Guji Natural"], "model": "m",
         "definition": verdict.DEFINITION_VERSION, "label": "coffee"}])  # fmt: skip
    lake.write("verdict_batches", "verdict_batches/state=submitted/x.parquet", [
        {"batch_id": "x", "state": "submitted", "at": at, "custom_id": by_title["Roasted Sunflower Seeds"],
         "model": "m", "definition": verdict.DEFINITION_VERSION}])  # fmt: skip
    assert verdict.pending(lake, "m", 100) == []
    assert len(verdict.pending(lake, "other-model", 100)) == 2  # verdicts are per model and definition


def test_submit_then_collect_stores_verdicts_once(lake, monkeypatch):
    _week(lake)
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    sent = {}

    class Batches:
        def create(self, requests):
            sent["reqs"] = requests
            return SimpleNamespace(id="batch1")

        def retrieve(self, batch_id):
            return SimpleNamespace(processing_status="ended")

        def results(self, batch_id):
            labels = {"Ethiopia Guji Natural": "coffee", "Roasted Sunflower Seeds": "not_coffee"}
            for r in sent["reqs"]:
                title = r["params"]["messages"][0]["content"].split("\n")[0].removeprefix("Title: ")
                yield SimpleNamespace(
                    custom_id=r["custom_id"],
                    result=SimpleNamespace(type="succeeded", message=_msg(labels[title])),
                )

    import anthropic

    monkeypatch.setattr(
        anthropic, "Anthropic", lambda: SimpleNamespace(messages=SimpleNamespace(batches=Batches()))
    )
    assert verdict.submit(lake)["submitted"] == 2
    assert verdict.pending(lake, "m", 100) == []  # in flight
    assert verdict.collect(lake, "2026-10-20") == {"collected": 2}
    assert verdict.collect(lake, "2026-10-20") == {"collected": 0}  # collected batches aren't read again
    con = lake.connect()
    assert con.execute(f"select count(*) from {lake.scan('verdicts')}").fetchone() == (2,)


def test_the_build_takes_claudes_verdict_over_the_rules(lake, tmp_path):
    _week(lake)
    todo = {r["title"]: r["input_hash"] for r in verdict.pending(lake, "m", 100)}
    at = datetime(2026, 10, 20, tzinfo=UTC)
    rows = [{"decided_at": at, "input_hash": todo["Roasted Sunflower Seeds"], "model": "m",
             "definition": verdict.DEFINITION_VERSION, "label": "not_coffee"}]  # fmt: skip
    lake.write_partition("verdicts", "2026-10-20", "b1", rows)

    rules_only = build(lake, tmp_path / "rules")
    assert {r[0] for r in rules_only.execute("select title from dim_coffee").fetchall()} == {
        "Ethiopia Guji Natural",
        "Roasted Sunflower Seeds",
    }
    with_claude = build(
        lake, tmp_path / "claude", verdict_model="m", verdict_definition=verdict.DEFINITION_VERSION
    )
    assert {r[0] for r in with_claude.execute("select title from dim_coffee").fetchall()} == {
        "Ethiopia Guji Natural"
    }
    by = dict(with_claude.execute("select classified_by, count(*) from stg_products group by 1").fetchall())
    assert by["claude"] == 1
