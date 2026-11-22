"""Claude's decisions on the roaster screen's undecided places (src/coffeedata/screen_verdict.py; the design's 4.3).

No model is called: every test uses a stub client that records the batches it is given and answers from a script.
Every place is made up."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import anthropic
import httpx
import pyarrow.parquet as pq
import pytest

from coffeedata import cli, control, raw_contracts, screen_verdict
from coffeedata.lake import SCHEMAS, Lake

RUN = "2026-12-02"
AT = datetime(2026, 12, 1, 7, tzinfo=UTC)
NOW = datetime(2026, 12, 2, 8, tzinfo=UTC)  # a Wednesday
SAME_WEEK = datetime(2026, 12, 5, 8, tzinfo=UTC)
NEXT_WEEK = datetime(2026, 12, 9, 8, tzinfo=UTC)


def _screen(sid: str, rid: str, home: str = "We serve coffee.") -> dict:
    return {"run_date": AT.date(), "batch_id": "b1", "screen_id": sid, "screened_at": AT, "roaster_id": rid,
            "state": "MI", "status": "ok", "platform": "shopify", "home_title": f"{rid} home", "home_text": home,
            "products_read": 2, "page_full": False, "page_timeout": False}  # fmt: skip


def _product(sid: str, rid: str, i: int, vendor: str = "") -> dict:
    return {"screen_id": sid, "roaster_id": rid, "product_id": str(i), "handle": f"h{i}", "title": f"House Blend {i}",
            "vendor": vendor or rid, "product_type": "Coffee", "tags": ["coffee"],
            "variants": [{"title": "12 oz", "grams": 340, "price": 18.0, "currency": "USD", "available": True}],
            "body_hash": "x", "body_text": "Chocolate."}  # fmt: skip


@pytest.fixture
def seeded(lake: Lake) -> Lake:
    places = ["a.test", "b.test", "c.test", "d.test"]
    lake.write("candidates", "candidates/source=overture/x.parquet", [
        {"added_at": AT, "source": "overture-2026-10-21.0", "roaster_id": p, "name": p.split(".")[0].title() + " Cafe",
         "city": "", "state": "MI", "website": f"https://{p}/", "locations": 1, "confidence": 0.9, "tags": ["cafe"],
         "roast_in_name": False} for p in places])  # fmt: skip
    lake.write_partition("screens", "2026-12-01", "b1", [_screen(f"s-{p[0]}", p) for p in places])
    lake.write_partition("screen_products", "2026-12-01", "b1",
                         [_product(f"s-{p[0]}", p, i) for p in places for i in (1, 2)])  # fmt: skip
    return lake


# screen_scores rows as the served build has them: a (rule 5) review, a reseller doubt, a suggested place, a review
SCORES = [
    {"screen_id": "s-a", "roaster_id": "a.test", "tier": "review", "reasons": ["named 2/2", "bags 340g"]},
    {
        "screen_id": "s-b",
        "roaster_id": "b.test",
        "tier": "review",
        "reasons": ["other_roaster 2/2", "named 2/2"],
    },
    {
        "screen_id": "s-c",
        "roaster_id": "c.test",
        "tier": "suggested",
        "reasons": ["named 6/6", "roast_words"],
    },
    {"screen_id": "s-d", "roaster_id": "d.test", "tier": "review", "reasons": ["page_timeout"]},
]


def _msg(label: str | None = "roaster_online", reason: str = "Its own bags.", stop: str = "end_turn"):
    text = json.dumps({"label": label, "reason": reason}) if label else "not json"
    usage = SimpleNamespace(input_tokens=900, cache_read_input_tokens=700, cache_creation_input_tokens=0,
                            output_tokens=40)  # fmt: skip
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)], usage=usage)


class Batches:
    def __init__(self, answers: dict | None = None, status: str = "ended", broken: set | None = None):
        self.created: list[list] = []
        self.answers, self.status, self.broken = answers or {}, status, broken or set()

    def create(self, requests):
        self.created.append(list(requests))
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}")

    def _check(self, batch_id):
        if batch_id in self.broken:
            raise anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.example.invalid/"))

    def retrieve(self, batch_id):
        self._check(batch_id)
        return SimpleNamespace(processing_status=self.status)

    def results(self, batch_id):
        self._check(batch_id)
        n = int(batch_id.rsplit("_", 1)[1])
        for req in self.created[n - 1]:
            sid = req["custom_id"]
            msg = self.answers.get(sid, _msg())
            kind = "errored" if msg is None else "succeeded"
            yield SimpleNamespace(custom_id=sid, result=SimpleNamespace(type=kind, message=msg))


def stub(**kw) -> SimpleNamespace:
    return SimpleNamespace(messages=SimpleNamespace(batches=Batches(**kw)))


def _rows(lake: Lake, table: str) -> list[dict]:
    return [r for p in sorted((lake.root / table).rglob("*.parquet")) if "schema" not in p.name
            for r in pq.read_table(p).to_pylist()]  # fmt: skip


# --- who is asked, and what they see ---------------------------------------------------------------------------------


def test_only_the_rules_undecided_review_places_are_asked():
    assert screen_verdict.undecided("review", ["named 2/2"]) and screen_verdict.undecided(
        "review", ["page_timeout"]
    )
    # a reseller doubt always goes to a person; a decided tier is never asked
    for reasons in (["other_roaster 1/2"], ["vendors 4"], ["serves:Proudly serving X"], ["wholesale 90%"]):
        assert not screen_verdict.undecided("review", reasons)
    assert not screen_verdict.undecided("suggested", ["named 6/6"]) and not screen_verdict.undecided(
        "dropped", []
    )


def test_pending_takes_the_undecided_with_their_stored_evidence_only(seeded):
    got = screen_verdict.pending(seeded, "m", SCORES)
    assert [r["screen_id"] for r in got] == ["s-a", "s-d"]
    text = got[0]["evidence"]
    assert (
        "Domain: a.test" in text
        and "Name: A Cafe" in text
        and "House Blend 1" in text
        and "12 oz (340 g)" in text
    )
    # nothing the screen or the rules decided is shown: no status, platform, tier or reason code
    for word in ("status", "shopify", "review", "named 2/2", "tier"):
        assert word not in text.lower()
    assert got[0]["evidence_hash"] == screen_verdict.evidence_hash(text)
    assert screen_verdict.pending(seeded, "m", SCORES) == got  # the same evidence every time


def test_the_evidence_is_capped_as_the_design_says():
    screen = _screen("s", "z.test", home="x" * 5000)
    products = [_product("s", "z.test", i) for i in range(60)]
    text = screen_verdict.evidence(screen, products, None)
    assert "x" * 2000 in text and "x" * 2001 not in text
    assert text.count("| vendor:") == 40 and "the first 40 of 60" in text


def test_a_prompt_change_is_a_new_version():
    v = screen_verdict.DEFINITION_VERSION
    assert v == screen_verdict.definition_version()
    assert screen_verdict.definition_version(prompt="screen-verdict-2") != v
    assert screen_verdict.definition_version(system=screen_verdict.SYSTEM + " ") != v
    assert screen_verdict.definition_version(schema={**screen_verdict.SCHEMA, "required": ["label"]}) != v


# --- submit and collect ----------------------------------------------------------------------------------------------


def test_submit_and_collect_write_exactly_the_contracts_columns(seeded, monkeypatch):
    monkeypatch.delenv(screen_verdict.MODEL_ENV, raising=False)
    client = stub(answers={"s-d": _msg("unsure", "Bags under its name, nothing says who roasts.")})
    out = screen_verdict.submit(seeded, client=client, cap=10, scores=SCORES, now=NOW)
    assert (out["pending"], out["submitted"], out["batch_ids"], out["over_cap"]) == (2, 2, ["msgbatch_1"], 0)
    (reqs,) = client.messages.batches.created
    req = reqs[0]["params"]
    assert (
        req["model"] == "claude-haiku-5-5"
        and req["output_config"]["format"]["schema"] == screen_verdict.SCHEMA
    )
    assert req["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert [r["custom_id"] for r in reqs] == ["s-a", "s-d"]
    got = screen_verdict.collect(seeded, RUN, client=client)
    assert got["collected"] == 2 and got["usage"]["output_tokens"] == 80
    batches, verdicts = _rows(seeded, "screen_verdict_batches"), _rows(seeded, "screen_verdicts")
    assert list(batches[0]) == SCHEMAS["screen_verdict_batches"].names
    assert (
        list(verdicts[0])
        == SCHEMAS["screen_verdicts"].names
        == raw_contracts.columns("screen", "screen_verdicts")
    )
    by = {v["screen_id"]: v for v in verdicts}
    assert (by["s-a"]["label"], by["s-d"]["label"]) == ("roaster_online", "unsure")  # unsure is a fine answer
    assert {v["definition"] for v in verdicts} == {screen_verdict.DEFINITION_VERSION}
    assert by["s-a"]["roaster_id"] == "a.test" and by["s-a"]["evidence_hash"]
    assert {v["label"] for v in verdicts} <= set(
        raw_contracts.CONTRACTS["screen"]["values"]["screen_verdicts.label"]
    )
    # decided now: nothing is pending, and a second collect finds nothing open
    assert screen_verdict.pending(seeded, "claude-haiku-5-5", SCORES) == []
    assert screen_verdict.collect(seeded, RUN, client=client) == {"collected": 0}


def test_a_second_run_adds_files_and_rewrites_none(seeded, monkeypatch):
    client = stub()
    screen_verdict.submit(seeded, client=client, cap=1, scores=SCORES, now=NOW)
    screen_verdict.collect(seeded, RUN, client=client)
    files = {p: p.read_bytes() for t in ("screen_verdict_batches", "screen_verdicts")
             for p in (seeded.root / t).rglob("*.parquet")}  # fmt: skip
    screen_verdict.submit(seeded, client=client, cap=1, scores=SCORES, now=NEXT_WEEK)
    screen_verdict.collect(seeded, "2026-12-09", client=client)
    after = {p: p.read_bytes() for t in ("screen_verdict_batches", "screen_verdicts")
             for p in (seeded.root / t).rglob("*.parquet")}  # fmt: skip
    assert files.items() <= after.items() and len(after) > len(files)
    assert [v["screen_id"] for v in _rows(seeded, "screen_verdicts")] == [
        "s-a",
        "s-d",
    ]  # one each, never twice


def test_the_weekly_cap_bounds_what_is_sent(seeded):
    client = stub(status="in_progress")
    out = screen_verdict.submit(seeded, client=client, cap=1, scores=SCORES, now=NOW)
    assert (out["submitted"], out["over_cap"]) == (1, 1)
    # the same week: the cap is spent, so nothing more goes, and an open batch isn't sent twice
    out = screen_verdict.submit(seeded, client=client, cap=1, scores=SCORES, now=SAME_WEEK)
    assert (out["room"], out["submitted"], out["pending"]) == (0, 0, 1)
    assert screen_verdict.sent_this_week(seeded, date(2026, 12, 6)) == 1
    # next week the cap starts again
    assert screen_verdict.submit(seeded, client=client, cap=1, scores=SCORES, now=NEXT_WEEK)["submitted"] == 1


def test_a_refusal_an_error_or_a_reply_outside_the_schema_leaves_the_place_pending(seeded):
    client = stub(answers={"s-a": _msg(stop="refusal"), "s-d": _msg(label=None)})
    screen_verdict.submit(seeded, client=client, cap=10, scores=SCORES, now=NOW)
    got = screen_verdict.collect(seeded, RUN, client=client)
    assert (got["collected"], got["unparsed"]) == (0, 2)
    assert [r["screen_id"] for r in screen_verdict.pending(seeded, screen_verdict.model(), SCORES)] == [
        "s-a",
        "s-d",
    ]
    assert screen_verdict.parse(_msg(reason="x" * 999))[1] == "x" * screen_verdict.REASON_CHARS


def test_a_batch_still_running_is_left_open(seeded):
    client = stub(status="in_progress")
    screen_verdict.submit(seeded, client=client, cap=10, scores=SCORES, now=NOW)
    assert screen_verdict.collect(seeded, RUN, client=client) == {"collected": 0, "not_ended": 1}


# --- the command: errors are visible, the build is not touched -------------------------------------------------------


def test_the_command_names_a_batch_it_could_not_collect_and_fails_after_sending_the_rest(
    seeded, monkeypatch, capsys
):
    client = stub(broken={"msgbatch_1"})
    screen_verdict.submit(
        seeded, client=client, cap=1, scores=SCORES, now=NOW
    )  # one open batch the API can't read
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-for-the-stub")
    monkeypatch.setattr("coffeedata.extract.client", lambda: client)
    monkeypatch.setattr(screen_verdict, "served_scores", lambda lake: SCORES)
    args = argparse.Namespace(run_date=RUN, cap=10, dry_run=False)
    with pytest.raises(SystemExit, match=r"msgbatch_1 \(APIConnectionError\)"):
        cli.cmd_screen_verdicts(args)
    out = capsys.readouterr().out
    error = json.loads(next(line for line in out.splitlines() if '"severity": "ERROR"' in line))
    assert error["batches"][0]["batch_id"] == "msgbatch_1"
    assert len(client.messages.batches.created) == 2  # the submit still ran, for the place left
    [end] = [
        r for r in control.rows(seeded, RUN) if r["job"] == "screen-verdicts" and r["status"] != "running"
    ]
    assert end["status"] == "failed"


def test_the_command_refuses_without_a_key_and_dry_runs_without_one(seeded, monkeypatch, capsys):
    for var in ("COFFEEDATA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(screen_verdict, "served_scores", lambda lake: SCORES)
    with pytest.raises(SystemExit, match="needs Anthropic credentials"):
        cli.cmd_screen_verdicts(argparse.Namespace(run_date=RUN, cap=None, dry_run=False))
    cli.cmd_screen_verdicts(argparse.Namespace(run_date=RUN, cap=None, dry_run=True))
    assert "2 places pending, 1000 left in this week's cap" in capsys.readouterr().out


def test_no_scores_served_means_nothing_pending(lake):
    assert screen_verdict.served_scores(lake) == [] and screen_verdict.pending(lake, "m") == []
    assert date.fromisoformat(RUN)  # the run date the tests use is a real day
