from datetime import UTC, date, datetime
from types import SimpleNamespace

import anthropic
import httpx

from coffeedata import enrich

from .lakehelp import product, run

LLM_JSON = ('{"origin_country": "Kenya", "origin_region": null, "producer": null, "process": "washed", '
            '"varietals": [], "elevation_masl_min": null, "elevation_masl_max": null, "roast_level": null, '
            '"tasting_notes": ["blackcurrant"], "is_blend": false, "is_decaf": false}')


def msg(text=LLM_JSON, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


def seed(lake, n=3):
    rid = "x.com"
    run(lake, "2026-10-26", rid, [product(str(i)) for i in range(n)])
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    lake.write_partition("bodies", "2026-10-26", "b", [
        {"body_hash": f"b{i}", "body_text": f"Body {i}", "first_seen_run": date(2026, 10, 26)} for i in range(n)])
    lake.write_partition("enrichments", "2026-10-26", "r", [
        {"extracted_at": now, "roaster_id": rid, "product_id": str(i), "body_hash": f"b{i}", "method": "rules",
         "needs_llm": True, "origin_country": "Kenya" if i == 0 else None} for i in range(n)])


class FakeClient:
    def __init__(self, fail_ids=()):
        self.calls, self.fail_ids = 0, set(fail_ids)
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **req):
        self.calls += 1
        body = req["messages"][0]["content"]
        if any(f"Body {i}" in body for i in self.fail_ids):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
        return msg()


def test_pending_is_derived_and_failures_retry(lake, monkeypatch):
    seed(lake)
    assert len(enrich.pending(lake, 10)) == 3
    assert len(enrich.pending(lake, 2)) == 2  # the cap leaves the rest pending, not "done"
    client = FakeClient(fail_ids={1})
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    out = enrich.run(lake, limit=10, run_date="2026-10-26")
    assert out == {"pending": 3, "enriched": 2, "errors": 1}  # connection error didn't abort the run
    left = enrich.pending(lake, 10)
    assert [p["product_id"] for p in left] == ["1"]  # only the failed one comes back
    rows = lake.connect().execute(f"select product_id, origin_country, process from {lake.scan('enrichments')} "
                                  "where method = 'llm' order by 1").fetchall()
    assert rows == [("0", "Kenya", "washed"), ("2", "Kenya", "washed")]


def test_batches_in_flight_are_not_resubmitted_and_collect(lake, monkeypatch):
    seed(lake, n=3)
    monkeypatch.setattr(enrich, "SYNC_LIMIT", 1)
    results = {}

    class Batches:
        def create(self, requests):
            results["ids"] = [r["custom_id"] for r in requests]
            return SimpleNamespace(id="msgbatch_1")

        def retrieve(self, batch_id):
            return SimpleNamespace(processing_status="ended")

        def results(self, batch_id):
            ok = [SimpleNamespace(custom_id=c, result=SimpleNamespace(type="succeeded", message=msg()))
                  for c in results["ids"][:2]]
            return ok + [SimpleNamespace(custom_id=results["ids"][2], result=SimpleNamespace(type="errored"))]

    client = SimpleNamespace(messages=SimpleNamespace(batches=Batches()))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    assert enrich.run(lake, 10, "2026-10-26")["submitted"] == 3
    assert enrich.pending(lake, 10) == []  # in flight
    assert enrich.collect(lake, "2026-10-26") == {"collected": 2}
    assert len(enrich.pending(lake, 10)) == 1  # the errored item is pending again
    assert enrich.collect(lake, "2026-10-26") == {"collected": 0}  # collected once, never twice
