"""Claude's coffee-or-not verdicts: what gets sent, how answers are stored, and how the build uses them."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

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
    monkeypatch.setattr(verdict, "submit", lambda lk, n: seen.append("submit") or {"submitted": 0})
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
    assert build and build.group(1).startswith(
        "rc=0; coffeedata classify --wait 45 || rc=1; coffeedata build || rc=1;"
    )
    assert build.group(1).endswith("exit $rc")


# --- never serve a build without verdicts (the 2026-11-09 release) ----------------------------------------------

GATE = "test.coffeedata.verdicts_cover_catalog"  # as dbt names the verdict gate in run_results


def _record(lake, build_id: str, model: str, require, gate: str | None, **columns):
    """A passing build record as `coffeedata build` writes it: its verdict row, the dbt vars in run_results, and the
    verdict gate's node (absent when `gate` is None, as in the image without it)."""
    now = datetime(2026, 11, 10, 6, 21, tzinfo=UTC)
    base = {"build_id": build_id, "built_at": now} | columns
    dbt_vars = {"verdict_model": model, "verdict_definition": verdict.DEFINITION_VERSION}
    if require is not None:
        dbt_vars["require_verdicts"] = require
    results = json.dumps({"args": {"vars": dbt_vars}, "results": []})
    rows = [
        base | {"kind": "build", "unique_id": "build", "status": "pass"},
        base | {"kind": "artifact", "unique_id": "run_results.build.json", "payload": results},
    ]
    if gate is not None:
        rows.append(base | {"kind": "node", "unique_id": GATE, "status": gate})
    lake.write("builds", f"builds/run_date=2026-11-09/build={build_id}.parquet", rows)


def test_a_release_without_checked_verdicts_is_never_served(lake, monkeypatch):
    """The 11-09 build: an image without the verdict model, whose dbt vars had verdict_model '' and no
    require_verdicts, and no verdicts_cover_catalog node. Every test it had passed, and it was served for a week."""
    import pytest

    from coffeedata import quality

    _no_verdicts(monkeypatch)
    _record(lake, "b0909", "", None, None)  # as recorded on 2026-11-10
    _record(lake, "bgood", "m", True, "pass")
    _record(lake, "bnogate", "m", True, None)
    _record(lake, "boff", "m", False, "pass")  # the gate ran with require_verdicts off: it checked nothing
    _record(lake, "bnamed", "", False, "pass", rules_only_override="--allow-rules-only")

    with pytest.raises(ValueError, match=r"can't be served: no verdict model"):
        quality.serve(lake, "b0909", "build for 2026-11-09")
    for b, why in (("bnogate", "verdicts_cover_catalog did not run"), ("boff", "the verdict gate was off")):
        with pytest.raises(ValueError, match=why):
            quality.servable(lake, b)
    assert quality.servable(lake, "bgood") is None
    assert (
        quality.servable(lake, "bnamed") is None
    )  # its record names the override (and the serving log has it)
    assert quality.servable(lake, "b0909", "rollback --allow-rules-only (fork)") == (
        "rollback --allow-rules-only (fork)"
    )
    monkeypatch.setenv("COFFEEDATA_ALLOW_RULES_ONLY", "1")
    assert quality.servable(lake, "b0909") == "COFFEEDATA_ALLOW_RULES_ONLY=1"
    assert not (lake.root / "marts" / "release.parquet").exists()  # nothing was served


def test_record_build_fails_a_build_whose_verdicts_werent_checked(lake, tmp_path, monkeypatch, caplog):
    from coffeedata import quality

    def results(model: str, require, gate: str | None) -> list[dict]:
        nodes = [{"unique_id": GATE, "status": gate}] if gate else []
        return [{"args": {"vars": {"verdict_model": model, "require_verdicts": require}}, "results": nodes}]

    # No release here: record_build's verdict below is the verdict guard's alone. The suite allows rules only by
    # default (conftest); production doesn't, so neither does this test.
    monkeypatch.delenv("COFFEEDATA_ALLOW_RULES_ONLY", raising=False)
    monkeypatch.setattr(quality, "RELEASE_TABLES", ())
    args = (lake, tmp_path / "missing.duckdb", "2026-11-09", tmp_path / "none.json")
    digest = "r/ingest@sha256:" + "a" * 64
    info = {"verdict_model": "m", "verdict_definition": verdict.DEFINITION_VERSION, "configured_image": digest,
            "verdicts_in_flight": 7}  # fmt: skip
    assert quality.record_build(*args, results("m", True, "pass"), True, "ok", verdicts=info) is True
    old = {"verdict_model": ""}
    assert quality.record_build(*args, results("", None, None), True, "old", verdicts=old) is False
    assert "can't be served: no verdict model" in caplog.text
    assert quality.record_build(*args, results("m", True, None), True, "nogate", verdicts=info) is False
    named = info | {"verdict_model": "", "rules_only_override": "--allow-rules-only"}
    assert quality.record_build(*args, results("", False, "pass"), True, "named", verdicts=named) is True
    rec = {
        r[0]: r[1:]
        for r in lake.connect()
        .execute(
            f"select build_id, status, verdict_model, verdict_definition, configured_image, rules_only_override, "
            f"verdicts_in_flight from {lake.scan('builds')} where kind = 'build'"
        )
        .fetchall()
    }
    assert rec["ok"] == ("pass", "m", verdict.DEFINITION_VERSION, digest, None, 7)
    assert rec["old"][0] == "fail" and rec["nogate"][0] == "fail"
    assert rec["named"][0] == "pass" and rec["named"][4] == "--allow-rules-only"
    assert quality.servable(lake, "ok") is None


def test_the_build_records_its_verdict_inputs_and_override(lake, monkeypatch, tmp_path):
    import argparse

    from coffeedata import cli, quality

    seen = {}
    monkeypatch.setattr(quality, "record_build", lambda *a, **k: seen.update(k) or False)
    monkeypatch.setattr(cli, "_write_personal_marts", lambda *a: None)
    monkeypatch.setattr(verdict, "in_flight", lambda lk: 12)
    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    monkeypatch.setenv("COFFEEDATA_INGEST_IMAGE", "r/ingest@sha256:" + "b" * 64)
    db = str(tmp_path / "coffee.duckdb")
    build = lambda **kw: lambda cli: cli.cmd_build(argparse.Namespace(db=db, run_date="2026-10-19", **kw))  # noqa: E731
    assert _dbt_vars(monkeypatch, tmp_path, build())["require_verdicts"] is True
    assert seen["verdicts"] == {
        "verdict_model": "m", "verdict_definition": verdict.DEFINITION_VERSION,
        "configured_image": "r/ingest@sha256:" + "b" * 64, "rules_only_override": None, "verdicts_in_flight": 12,
    }  # fmt: skip
    _dbt_vars(monkeypatch, tmp_path, build(allow_rules_only=True))
    assert seen["verdicts"]["rules_only_override"] == "--allow-rules-only"


def test_the_site_refuses_a_served_release_without_checked_verdicts(lake, monkeypatch, tmp_path):
    import argparse

    import pytest

    from coffeedata import cli, quality

    _no_verdicts(monkeypatch)
    _record(lake, "b0909", "", None, None)
    monkeypatch.setattr(quality, "served", lambda lk: {"build_id": "b0909", "run_date": "2026-11-09"})
    monkeypatch.setattr(quality, "materialize", lambda *a: pytest.fail("materialized"))
    ns = argparse.Namespace(
        served=True, skip_if_unserved=True, db=str(tmp_path / "s.duckdb"), out=str(tmp_path)
    )
    with pytest.raises(SystemExit, match="isn't rendered: release b0909 can't be served: no verdict model"):
        cli.cmd_site(ns)


def test_rollback_to_a_release_without_verdicts_needs_a_named_reason(lake, monkeypatch):
    import pytest

    from coffeedata import cli, quality

    _no_verdicts(monkeypatch)
    got = []
    dbs = []  # rollback serves into cli.SERVED_DB, never the cwd-relative default
    monkeypatch.setattr(quality, "rollback", lambda lk, to, reason, db=None, rules_only_override=None: dbs.append(
        db) or got.append(rules_only_override) or "b0")  # fmt: skip
    monkeypatch.setattr(cli, "cmd_publish", lambda a: None)
    monkeypatch.setattr(cli, "_export_and_site", lambda *a: None)
    with pytest.raises(SystemExit, match="needs --reason"):
        cli.main(["rollback", "--to", "b0", "--allow-rules-only"])
    cli.main(["rollback", "--to", "b0", "--allow-rules-only", "--reason", "fork without a key"])
    cli.main(["rollback", "--to", "b0"])
    assert got == ["rollback --allow-rules-only (fork without a key)", None]
    assert dbs == [cli.SERVED_DB, cli.SERVED_DB]


def test_classify_waits_for_its_batch_then_says_what_is_still_open(lake, monkeypatch, capsys):
    """The build job's classify submitted at 06:19:10 and the build began at 06:19:26: that week's verdicts were
    never in it. With --wait it collects them; past the wait it says so and the build counts what is missing."""
    from coffeedata import cli

    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(verdict, "collect", lambda lk, d=None: {"collected": 0})
    monkeypatch.setattr(verdict, "submit", lambda lk, n: {"submitted": 3, "batch_ids": ["mb1"]})
    monkeypatch.setattr(verdict, "open_batch_ids", lambda lk: {"mb1"})
    cli.main(["classify", "--wait", "2"])
    out = capsys.readouterr()
    assert json.loads(out.out)["still_open"] == ["mb1"]
    assert "1 verdict batch(es) still open after 2 min" in out.err
    assert clock[0] == 120  # it waited the full two minutes


def test_a_failed_read_during_the_wait_is_retried_not_fatal(lake, monkeypatch, capsys, caplog):
    """The wait reads the lake every minute; on hf:// a read can 429 or time out. One failure is logged and counted,
    the next minute tries again, and the batch is still collected within the wait."""
    import duckdb

    from coffeedata import cli

    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    reads = []

    def collect(lk, d=None):
        reads.append(clock[0])
        if clock[0] == 60:  # the first read of the wait fails
            raise duckdb.HTTPException("HTTP 429 (Too Many Requests)")
        return {"collected": 3 if clock[0] == 120 else 0}

    monkeypatch.setattr(verdict, "collect", collect)
    monkeypatch.setattr(verdict, "submit", lambda lk, n: {"submitted": 3, "batch_ids": ["mb1"]})
    monkeypatch.setattr(verdict, "open_batch_ids", lambda lk: set() if clock[0] >= 120 else {"mb1"})
    cli.main(["classify", "--wait", "5"])  # returns: exit 0
    out = capsys.readouterr()
    got = json.loads(out.out)
    assert got["wait_read_failures"] == 1 and got["collect_after_wait"] == 3 and "still_open" not in got
    assert "a read during the wait failed, retrying next minute: HTTP 429" in caplog.text
    assert clock[0] == 120  # it stopped waiting once the batch was collected

    # Every read failing: the wait runs out, and classify goes on, saying what is still open.
    clock[0] = 0.0
    monkeypatch.setattr(verdict, "collect", lambda lk, d=None: {"collected": 0} if clock[0] == 0 else 1 / 0)
    cli.main(["classify", "--wait", "3"])
    got = json.loads(capsys.readouterr().out)
    assert got["wait_read_failures"] == 3 and got["still_open"] == ["mb1"]


def test_rollback_skips_releases_that_cant_be_served(lake, monkeypatch):
    """After the guard, a plain `rollback` from the 11-16 release would pick 11-09, which can't be served (no verdict
    model). It picks the newest earlier release that can be, and says plainly when none can."""
    import pytest

    from coffeedata import quality

    _no_verdicts(monkeypatch)
    _record(lake, "b1026", "m", True, "pass")
    _record(lake, "b1102", "", None, None)
    _record(lake, "b1109", "m", True, None)  # the gate never ran
    _record(lake, "b1116", "m", True, "pass")
    history = ["b1026", "b1102", "b1109", "b1116"]
    monkeypatch.setattr(quality, "builds", lambda lk: [(b, "2026-11-16", True) for b in history])
    monkeypatch.setattr(quality, "served", lambda lk: {"build_id": "b1116"})
    served = []
    monkeypatch.setattr(
        quality, "serve", lambda lk, b, reason, **kw: served.append((b, kw["rules_only_override"]))
    )
    assert quality.rollback(lake) == "b1026"
    # With a named override, the newest earlier release is servable again (its record still says why).
    assert quality.rollback(lake, rules_only_override="rollback --allow-rules-only (fork)") == "b1109"
    assert served == [("b1026", None), ("b1109", "rollback --allow-rules-only (fork)")]
    history.remove("b1026")
    with pytest.raises(ValueError, match=r"no earlier passing release can be served: 2 passed their checks"):
        quality.rollback(lake)
    assert len(served) == 2


def test_in_flight_counts_versions_awaiting_this_models_verdict(lake, monkeypatch):
    _no_verdicts(monkeypatch)
    assert verdict.in_flight(lake) == 0  # no model: nothing is awaited
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    at = datetime(2026, 11, 17, 6, 19, tzinfo=UTC)
    row = {"at": at, "model": "m", "definition": verdict.DEFINITION_VERSION}
    lake.write("verdict_batches", "verdict_batches/state=submitted/a.parquet", [
        row | {"batch_id": "a", "state": "submitted", "custom_id": h} for h in ("h1", "h2")])  # fmt: skip
    lake.write("verdict_batches", "verdict_batches/state=submitted/b.parquet", [
        row | {"batch_id": "b", "state": "submitted", "custom_id": "h3"}])  # fmt: skip
    lake.write("verdict_batches", "verdict_batches/state=collected/b.parquet", [
        row | {"batch_id": "b", "state": "collected", "custom_id": "h3"}])  # fmt: skip
    lake.write("verdict_batches", "verdict_batches/state=submitted/c.parquet", [
        row | {"batch_id": "c", "state": "submitted", "custom_id": "h4", "model": "other"}])  # fmt: skip
    assert verdict.in_flight(lake) == 2


def test_terraform_build_job_waits_for_the_verdicts_and_knows_its_image():
    import re
    from pathlib import Path

    tf = (Path(__file__).parents[1] / "src/terraform/ingestion.tf").read_text()
    build = re.search(r'"build" = \{.*?args += \["(.*?)"\]', tf, re.S)
    assert build and build.group(1).startswith(
        "rc=0; coffeedata classify --wait 45 || rc=1; coffeedata build"
    )
    assert re.search(r'name += "COFFEEDATA_INGEST_IMAGE"\s+value += var\.ingest_image', tf)
    assert verdict.IMAGE_ENV == "COFFEEDATA_INGEST_IMAGE"
    # The wait fits: up to 90 minutes for the chain (07:47 from 06:17), 45 for the verdicts, inside 240.
    assert (
        'timeout         = "14400s"' in tf
        and '"build"       = { job = "merge,enrich", deadline_utc = "07:47" }' in tf
    )


REMOTE_LAKES = ("hf://datasets/someone/coffee-data", "s3://bucket/coffee-data", "gs://bucket/lake")


def test_rules_only_is_refused_on_a_remote_lake(lake, monkeypatch, tmp_path):
    """A remote lake is published and served, so no form of the rules-only override works there: the env var, the
    flag on classify, build and weekly, rollback's flag, a serve-time override or one a build record carries. Each
    exits naming the override before any work; on a local lake each still works."""
    from coffeedata import cli, quality

    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_ALLOW_RULES_ONLY", "1")  # conftest sets it too: the suite's local lakes
    assert verdict.rules_only_allowed() and verdict.rules_only_override() == "COFFEEDATA_ALLOW_RULES_ONLY=1"
    assert verdict.require(True) is False  # local: allowed

    worked = []
    monkeypatch.setattr(cli, "Lake", lambda *a, **k: worked.append("lake") or pytest.fail("work started"))
    for url in REMOTE_LAKES:
        monkeypatch.setenv("COFFEEDATA_LAKE", url)
        for call in (verdict.rules_only_allowed, verdict.rules_only_override, verdict.require):
            with pytest.raises(SystemExit, match="refused on a remote lake"):
                call()
        monkeypatch.delenv("COFFEEDATA_ALLOW_RULES_ONLY")
        assert verdict.rules_only_override() is None  # no override asked: nothing to refuse
        for call in (lambda: verdict.rules_only_override(True), lambda: verdict.require(True)):
            with pytest.raises(SystemExit, match=r"--allow-rules-only\) is refused on a remote lake"):
                call()
        for argv in (
            ["classify", "--allow-rules-only"],
            ["build", "--allow-rules-only"],
            ["weekly", "--allow-rules-only"],
        ):
            with pytest.raises(SystemExit, match="refused on a remote lake"):
                cli.main(argv)
        monkeypatch.setenv("COFFEEDATA_ALLOW_RULES_ONLY", "1")
    assert worked == []  # each refused before it opened the lake

    # serve, rollback and site --served all decide through quality.servable, on the lake's own URL.
    monkeypatch.delenv("COFFEEDATA_ALLOW_RULES_ONLY")
    for recorded in (None, "--allow-rules-only"):
        monkeypatch.setattr(quality, "verdict_check", lambda lk, b, r=recorded: ("no verdict model", r))
        for url in REMOTE_LAKES:
            remote = SimpleNamespace(url=url)
            with pytest.raises(SystemExit, match="refused on a remote lake"):
                quality.servable(remote, "b1", None if recorded else "rollback --allow-rules-only (why)")
        local = SimpleNamespace(url=str(tmp_path / "lake"))
        allowed = quality.servable(local, "b1", None if recorded else "rollback --allow-rules-only (why)")
        assert allowed == (None if recorded else "rollback --allow-rules-only (why)")


def test_no_job_workflow_or_terraform_sets_the_rules_only_override():
    """The override is for a local lake and tests: no GitHub workflow, Terraform file or Dockerfile sets it, so a
    production run can't ask for it (and on a remote lake coffeedata would refuse it anyway)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = [
        *(root / ".github/workflows").glob("*.y*ml"),
        *(root / "src/terraform").rglob("*.tf"),
        *(root / "src/terraform").rglob("*.tfvars*"),
        root / "app/Dockerfile",
        root / "ingest.Dockerfile",
        root / "ingest.cloudbuild.yaml",
    ]
    assert files
    setting = [str(f.relative_to(root)) for f in files if "ALLOW_RULES_ONLY" in f.read_text(errors="ignore")]
    assert setting == []


def _open_batch(lake, batch_id="msgbatch_x"):
    at = datetime(2026, 11, 17, 6, 19, tzinfo=UTC)
    lake.write("verdict_batches", f"verdict_batches/state=submitted/{batch_id}.parquet", [
        {"batch_id": batch_id, "state": "submitted", "at": at, "custom_id": "h1", "model": "m",
         "definition": verdict.DEFINITION_VERSION}])  # fmt: skip


def _refusing(monkeypatch, refuse):
    """Stand-in Batches: `retrieve` raises a 401 while refuse() is true, then the batch has ended with one verdict."""
    import anthropic
    import httpx

    class Batches:
        def create(self, requests):
            raise AssertionError("nothing is pending in these tests")

        def retrieve(self, batch_id):
            if refuse():
                req = httpx.Request("GET", "https://api.anthropic.com")
                raise anthropic.APIStatusError(
                    "invalid x-api-key", response=httpx.Response(401, request=req), body=None
                )
            return SimpleNamespace(processing_status="ended")

        def results(self, batch_id):
            yield SimpleNamespace(
                custom_id="h1", result=SimpleNamespace(type="succeeded", message=_msg("coffee"))
            )

    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=SimpleNamespace(batches=Batches()))
    )


def test_collect_names_the_batches_it_could_not_read_and_why(lake, monkeypatch):
    """The batch submitted on 2026-11-17 was never collected: collect logged the API's error, went on, and
    reported `collected: 0`, the same as a week with nothing to collect."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _open_batch(lake)
    _refusing(monkeypatch, lambda: True)
    out = verdict.collect(lake, "2026-11-24")
    assert out == {
        "collected": 0,
        "errors": [{"batch_id": "msgbatch_x", "error": "APIStatusError", "http_status": 401}],
    }
    assert verdict.open_batch_ids(lake) == {"msgbatch_x"}  # still open: the next collect asks again


def test_a_batch_claude_is_still_working_through_is_counted_not_an_error(lake, monkeypatch):
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _open_batch(lake)
    batches = SimpleNamespace(retrieve=lambda batch_id: SimpleNamespace(processing_status="in_progress"))
    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=SimpleNamespace(batches=batches))
    )
    assert verdict.collect(lake, "2026-11-24") == {"collected": 0, "not_ended": 1}


def test_classify_fails_when_a_batch_cannot_be_collected_and_says_which_and_why(lake, monkeypatch, capsys):
    """Like a rejected submit: the summary is printed, one ERROR line names the batch and the reason, and the exit is
    non-zero, so the build job's execution fails and emails. The build after it still runs (the job's `|| rc=1`)."""
    from coffeedata import cli

    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _open_batch(lake)
    _refusing(monkeypatch, lambda: True)
    monkeypatch.setattr(verdict, "submit", lambda lk, limit=0: {"pending": 0})
    with pytest.raises(
        SystemExit, match=r"collect failed for 1 batch\(es\): msgbatch_x \(APIStatusError, HTTP 401\)"
    ):
        cli.main(["classify"])
    printed = capsys.readouterr().out
    assert '"collect_failed"' in printed
    line = json.loads(printed.strip().splitlines()[-1])
    assert line["severity"] == "ERROR" and line["batches"][0]["batch_id"] == "msgbatch_x"
    assert "invalid x-api-key" not in line["message"]


def test_classify_passes_when_a_later_collect_reads_the_batch(lake, monkeypatch, capsys):
    """One refused read isn't a failure if the wait's next collect stores the batch."""
    import time

    from coffeedata import cli

    _no_verdicts(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_VERDICT_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _open_batch(lake)
    calls = []
    _refusing(monkeypatch, lambda: calls.append(1) or len(calls) == 1)
    monkeypatch.setattr(verdict, "submit", lambda lk, limit=0: {"pending": 0, "batch_ids": ["msgbatch_x"]})
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    cli.main(["classify", "--wait", "5"])  # returns: exit 0
    out = json.loads(capsys.readouterr().out)
    assert out["collect"]["errors"] and out["collect_after_wait"] == 1 and "collect_failed" not in out
