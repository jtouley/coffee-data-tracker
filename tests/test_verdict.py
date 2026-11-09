"""Claude's coffee-or-not verdicts: what gets sent, how answers are stored, and how the build uses them."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from coffeedata import verdict
from coffeedata.lake import new_batch_id

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
    assert req["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
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
            sent[f"batch{len(sent)}"] = requests
            return SimpleNamespace(id=f"batch{len(sent) - 1}")

        def retrieve(self, batch_id):
            return SimpleNamespace(processing_status="ended")

        def results(self, batch_id):
            labels = {"Ethiopia Guji Natural": "coffee", "Roasted Sunflower Seeds": "not_coffee"}
            for r in sent[batch_id]:
                title = r["params"]["messages"][0]["content"].split("\n")[0].removeprefix("Title: ")
                yield SimpleNamespace(
                    custom_id=r["custom_id"],
                    result=SimpleNamespace(type="succeeded", message=_msg(labels[title])),
                )

    import anthropic

    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=SimpleNamespace(batches=Batches()))
    )
    monkeypatch.setattr(verdict, "BATCH_SIZE", 1)  # large runs are split into several batches
    out = verdict.submit(lake)
    assert out["submitted"] == 2 and len(out["batch_ids"]) == 2
    assert verdict.pending(lake, "m", 100) == []  # in flight
    assert verdict.open_batch_ids(lake) == set(out["batch_ids"])
    assert verdict.collect(lake, "2026-10-20") == {"collected": 2}
    assert verdict.open_batch_ids(lake) == set()
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


def test_requests_cache_the_definition_and_batches_keep_it_an_hour():
    it = verdict.item({"title": "Kenya AA", "product_type": "", "tags": [], "grams": [340]})
    assert verdict.request(it, "m")["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert verdict.request(it, "m", ttl="1h")["system"][0]["cache_control"]["ttl"] == "1h"
    # The cached prefix must stay above the 512-token minimum for current models (about 4 characters a token);
    # a shorter one is silently not cached.
    assert len(verdict.SYSTEM) >= 4 * 512


def test_judge_warms_the_cache_with_one_request_before_running_in_parallel(monkeypatch):
    import threading

    import anthropic

    calls, first_done = [], threading.Event()

    class Messages:
        def create(self, **req):
            calls.append(first_done.is_set())
            first_done.set()
            return SimpleNamespace(
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=10,
                    cache_read_input_tokens=500,
                    cache_creation_input_tokens=0,
                    output_tokens=5,
                ),
                content=[SimpleNamespace(type="text", text=json.dumps({"label": "coffee"}))],
            )

    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=Messages()))
    rows = [{"title": f"Kenya {i}", "product_type": "", "tags": [], "grams": [340]} for i in range(5)]
    usage = {}
    assert set(verdict.judge(rows, "m", usage=usage).values()) == {True}
    assert calls[0] is False and all(calls[1:])  # one request ran before any other started
    assert usage["cache_read_input_tokens"] == 2500


# --- the guard: verdicts are required unless rules only is explicitly allowed (non-coffee-2026-11-04) -----------


def _no_verdicts(monkeypatch):
    for var in ("COFFEEDATA_ALLOW_RULES_ONLY", "COFFEEDATA_VERDICT_MODEL", "COFFEEDATA_ANTHROPIC_API_KEY",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):  # fmt: skip
        monkeypatch.delenv(var, raising=False)


def test_classify_fails_loudly_without_a_model_or_credentials(lake, monkeypatch, capsys):
    """The build job once ran a classify that printed a skip message and exited 0, so no release had verdicts."""
    import pytest

    from coffeedata import cli

    _no_verdicts(monkeypatch)
    with pytest.raises(SystemExit) as e:
        cli.main(["classify"])
    msg = str(e.value.code)
    assert (
        "COFFEEDATA_VERDICT_MODEL" in msg and "Anthropic credentials" in msg and "--allow-rules-only" in msg
    )
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")  # a model alone isn't enough
    with pytest.raises(SystemExit) as e:
        cli.main(["classify"])
    assert "Anthropic credentials" in str(e.value.code) and "no COFFEEDATA_VERDICT_MODEL" not in str(
        e.value.code
    )
    monkeypatch.delenv("COFFEEDATA_VERDICT_MODEL")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")  # nor credentials alone
    with pytest.raises(SystemExit, match="no COFFEEDATA_VERDICT_MODEL\\. "):
        cli.main(["classify"])


def test_rules_only_is_an_explicit_opt_out(lake, monkeypatch, capsys):
    from coffeedata import cli

    _no_verdicts(monkeypatch)
    cli.main(["classify", "--allow-rules-only"])  # returns: exit 0
    assert "rules only (explicitly allowed)" in capsys.readouterr().out
    for on in ("1", "true", "YES"):
        monkeypatch.setenv("COFFEEDATA_ALLOW_RULES_ONLY", on)
        assert verdict.require() is False
    for off in ("", "0", "no"):
        monkeypatch.setenv("COFFEEDATA_ALLOW_RULES_ONLY", off)
        assert not verdict.rules_only_allowed()


def test_classify_runs_when_configured_even_if_rules_only_is_allowed(lake, monkeypatch):
    from coffeedata import cli

    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_ALLOW_RULES_ONLY", "1")
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    seen = []
    monkeypatch.setattr(verdict, "collect", lambda lk, d=None: seen.append("collect") or {"collected": 0})
    monkeypatch.setattr(verdict, "submit", lambda lk, n, **_kw: seen.append("submit") or {"submitted": 0})
    cli.main(["classify", "--run-date", "2026-10-28"])
    assert seen == ["collect", "submit"]
    # The control plane's start and end rows (job classify), under the week's Monday.
    from coffeedata import control

    [row] = [r for r in control.rows(lake, "2026-10-26") if r["job"] == "classify"]
    assert row["status"] == "done" and row["code_version"]
    want = {"model": "m", "collect": {"collected": 0}, "submit": {"submitted": 0}}
    assert want.items() <= control.detail_of(row).items()


def test_classify_fails_when_claude_rejects_the_batches(lake, monkeypatch, capsys):
    """A key that is set but revoked passes the config check; the rejected submit must still fail the job."""
    import anthropic
    import httpx
    import pytest

    from coffeedata import cli

    _week(lake)
    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    class Batches:
        def create(self, requests):
            raise anthropic.APIError(
                "invalid x-api-key", httpx.Request("POST", "https://api.anthropic.com"), body=None
            )

    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=SimpleNamespace(batches=Batches()))
    )
    with pytest.raises(SystemExit, match="verdict batch submit failed: invalid x-api-key"):
        cli.main(["classify"])
    assert (
        '"error": "invalid x-api-key"' in capsys.readouterr().out
    )  # the JSON summary is still printed first
    from coffeedata import control

    [row] = [r for r in control.rows(lake) if r["job"] == "classify"]  # its end row says it failed, and why
    assert row["status"] == "failed" and control.detail_of(row)["submit"]["error"] == "invalid x-api-key"


def test_weekly_still_builds_after_a_failed_classify_and_then_fails(monkeypatch):
    import argparse

    import pytest

    from coffeedata import cli

    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    calls = []
    for name in ("cmd_probe", "cmd_ingest", "cmd_enrich", "cmd_publish", "_export_and_site"):
        monkeypatch.setattr(cli, name, lambda *a, n=name: calls.append(n))

    def classify(a):
        calls.append("cmd_classify")
        raise SystemExit("Claude's verdict batch submit failed: overloaded")

    monkeypatch.setattr(cli, "cmd_classify", classify)
    monkeypatch.setattr(cli, "cmd_build", lambda a: calls.append("cmd_build") or True)
    ns = argparse.Namespace(run_date="2026-11-09", discover=False, sample=None, enrich_limit=None,
                            db="build/coffee.duckdb", out="build/site", accept_volume_change=False)  # fmt: skip
    with pytest.raises(SystemExit, match="classify failed; the build ran on the verdicts already stored"):
        cli.cmd_weekly(ns)
    assert calls.index("cmd_classify") < calls.index("cmd_build") < calls.index("_export_and_site")


def test_weekly_fails_before_crawling_without_verdicts(monkeypatch):
    import argparse

    import pytest

    from coffeedata import cli

    _no_verdicts(monkeypatch)
    calls = []
    for name in ("cmd_discover", "cmd_probe", "cmd_ingest", "cmd_enrich", "cmd_classify", "cmd_build",
                 "cmd_publish"):  # fmt: skip
        monkeypatch.setattr(cli, name, lambda a, n=name: calls.append(n))
    ns = argparse.Namespace(run_date="2026-11-09", discover=False, sample=None, enrich_limit=None,
                            db="build/coffee.duckdb", out="build/site", accept_volume_change=False)  # fmt: skip
    with pytest.raises(SystemExit, match="COFFEEDATA_VERDICT_MODEL"):
        cli.cmd_weekly(ns)
    assert calls == []


def _dbt_vars(monkeypatch, tmp_path, fn) -> dict:
    """The vars `coffeedata build` passes to dbt, with dbt itself faked."""
    import subprocess

    from coffeedata import cli

    monkeypatch.setattr(cli, "DBT_TARGET", tmp_path / "target")
    argv = []
    monkeypatch.setattr(
        cli.subprocess, "run", lambda a, **kw: argv.append(a) or subprocess.CompletedProcess(a, 0)
    )
    fn(cli)
    dbt = next(a for a in argv if a[0] == "dbt")  # not the build's `git rev-parse` (its code_version)
    return json.loads(dbt[dbt.index("--vars") + 1])


def test_the_build_requires_verdicts_unless_rules_only_is_allowed(lake, monkeypatch, tmp_path):
    import argparse

    from coffeedata import cli, quality

    monkeypatch.setattr(quality, "record_build", lambda *a, **k: False)  # nothing to record from a faked dbt
    monkeypatch.setattr(cli, "_write_personal_marts", lambda *a: None)
    db = str(tmp_path / "coffee.duckdb")
    build = lambda **kw: lambda cli: cli.cmd_build(argparse.Namespace(db=db, run_date="2026-10-19", **kw))  # noqa: E731
    _no_verdicts(monkeypatch)
    assert _dbt_vars(monkeypatch, tmp_path, build())["require_verdicts"] is True
    assert _dbt_vars(monkeypatch, tmp_path, build(allow_rules_only=True))["require_verdicts"] is False
    monkeypatch.setenv("COFFEEDATA_ALLOW_RULES_ONLY", "1")
    assert _dbt_vars(monkeypatch, tmp_path, build())["require_verdicts"] is False


def _two_coffees(lake):
    probe(lake, "2026-10-18", RID)
    run(
        lake, "2026-10-19", RID, [product("A", title="Ethiopia Guji Natural"), product("K", title="Kenya AA")]
    )
    todo = {r["title"]: r["input_hash"] for r in verdict.pending(lake, "m", 100)}
    at = datetime(2026, 10, 20, tzinfo=UTC)
    return lambda *titles: lake.write_partition("verdicts", "2026-10-20", new_batch_id(), [
        {"decided_at": at, "input_hash": todo[t], "model": "m", "definition": verdict.DEFINITION_VERSION,
         "label": "coffee"} for t in titles])  # fmt: skip


def test_the_verdict_gate_fails_a_build_without_a_model_or_with_too_few_verdicts(lake, tmp_path):
    decide = _two_coffees(lake)
    gate = {"require_verdicts": True, "verdict_definition": verdict.DEFINITION_VERSION}

    r = build(lake, tmp_path / "none", check=False, **gate)  # no verdict model at all
    assert r.returncode != 0 and "FAIL 1 verdicts_cover_catalog" in r.stdout
    assert (
        build(lake, tmp_path / "off", check=False, verdict_model="").returncode == 0
    )  # gate off: dbt's default

    decide("Ethiopia Guji Natural")  # 1 of 2 listed coffees: 50% < 0.85
    r = build(lake, tmp_path / "half", check=False, verdict_model="m", **gate)
    assert r.returncode != 0 and "FAIL 1 verdicts_cover_catalog" in r.stdout
    assert (
        build(
            lake, tmp_path / "low", check=False, verdict_model="m", min_verdict_share=0.5, **gate
        ).returncode
        == 0
    )

    decide("Kenya AA")
    con = build(lake, tmp_path / "all", verdict_model="m", **gate)
    assert sorted(con.execute("select title, classified_by, verdict_model from dim_coffee").fetchall()) == [
        ("Ethiopia Guji Natural", "claude", "m"),
        ("Kenya AA", "claude", "m"),
    ]


def test_dim_coffee_says_what_classified_each_coffee(lake, tmp_path):
    from coffeedata import quality

    _two_coffees(lake)("Kenya AA")
    con = build(lake, tmp_path, verdict_model="m", verdict_definition=verdict.DEFINITION_VERSION)
    assert sorted(con.execute("select title, classified_by, verdict_model from dim_coffee").fetchall()) == [
        ("Ethiopia Guji Natural", "rules", None),
        ("Kenya AA", "claude", "m"),
    ]
    con.close()
    assert quality.verdict_coverage_message(tmp_path / "coffee.duckdb") == (
        "1 of 2 listed coffees (50.0%) decided by Claude's verdicts (m 1); the rest by the rules"
    )
    assert quality.verdict_coverage_message(tmp_path / "missing.duckdb") is None


def test_terraform_requires_the_verdict_model_and_the_build_job_fails_on_a_failed_classify():
    import re
    from pathlib import Path

    tf = (Path(__file__).parents[1] / "src/terraform/ingestion.tf").read_text()
    var = re.search(r'variable "verdict_model" \{(.*?)\n\}', tf, re.S)
    assert var and "default" not in var.group(1) and 'trimspace(var.verdict_model) != ""' in var.group(1)
    assert "for_each = each.value.claude ? [var.verdict_model] : []" in tf
    build = re.search(r'"build" = \{.*?args += \["(.*?)"\]', tf, re.S)
    assert build and build.group(1).startswith("rc=0; coffeedata classify || rc=1; coffeedata build || rc=1;")
    assert build.group(1).endswith("exit $rc")
