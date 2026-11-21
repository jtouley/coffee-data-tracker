"""Smoke, dbt (`make smoke`; CI's smoke job, part dbt). The recorded storefronts as a weekly run, through the path
`coffeedata build` takes: every model, then every gate test, the build's record and release, then serving it. Every
public_* table has rows, the public export records each file's sha256, and the contracts are what
`make contracts` writes and valid ODCS/ODPS."""

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

from coffeedata import cli, connectors, public, quality, raw_contracts

from .smoke import fixture_lake, require_dbt
from .test_contracts import CONTRACTS, ROOT, _validator

pytestmark = pytest.mark.smoke  # no module-wide dbt skip: the contracts test needs no dbt


def test_the_fixture_lake_builds_passes_the_gate_and_is_served(lake, tmp_path, monkeypatch, no_network):
    require_dbt()
    target = tmp_path / "dbt-target"  # this test's own dbt artifacts, not the checkout's dbt/target
    monkeypatch.setenv("DBT_TARGET_PATH", str(target))
    monkeypatch.setenv("DBT_LOG_PATH", str(tmp_path / "dbt-logs"))
    monkeypatch.setattr(cli, "DBT_TARGET", target)
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    week = connectors.week_of(cli._today())  # this week's run: fresh for latest_run_is_fresh
    counts = fixture_lake(lake, week)
    assert len(counts) == 5 and all(counts.values())

    db = tmp_path / "coffee.duckdb"
    cli.main(["build", "--db", str(db), "--run-date", week])  # exits 1 if any gate test fails

    # The build's record: it passed, and every gate test ran (none skipped by a crash between the two dbt steps).
    [(build_id, run_date, passed)] = quality.builds(lake)
    assert (run_date, passed) == (week, True)
    con = lake.connect()
    nodes = con.execute(
        f"select resource_type, status, count(*) from {lake.scan('builds')} where build_id = ? and kind = 'node' "
        "group by all",
        [build_id],
    ).fetchall()
    by_status = {(t, s): n for t, s, n in nodes}
    assert by_status.get(("test", "pass"), 0) >= 50, nodes
    assert not {s for _, s, _ in nodes} & {"fail", "error", "skipped"}, nodes

    # Every public_* table the build made has rows.
    built = duckdb.connect(str(db), read_only=True)
    tables = [
        t
        for (t,) in built.execute(
            "select table_name from information_schema.tables where table_name like 'public_%' "
            "and table_schema = 'main'"  # not a warn test's stored failures (main_dbt_test__audit)
        ).fetchall()
    ]
    assert set(public.PUBLIC_TABLES) <= set(tables)
    empty = [t for t in tables if built.execute(f"select count(*) from {t}").fetchone()[0] == 0]
    built.close()
    assert empty == []

    # The release is served: every release table is in the lake, and marts/ and the served db are this build.
    assert quality.served(lake)["build_id"] == build_id
    assert set(quality.release_files(lake, build_id)) == set(quality.RELEASE_TABLES)
    # The public export of the served release records which release it is and each file's sha256.
    manifest = public.export(cli.SERVED_DB, str(tmp_path / "public"))
    assert manifest["release_build_id"] == build_id and str(manifest["release_run_date"])[:10] == week
    written = json.loads((tmp_path / "public" / "manifest.json").read_text())
    for t in public.PUBLIC_TABLES:
        f = written["files"][f"{t}.parquet"]
        assert f["rows"] > 0 and f["sha256"] == public._sha256(tmp_path / "public" / f"{t}.parquet")

    # The ops tables, beside the release under ops/: exactly their contract's columns and types (lake.SCHEMAS) from
    # the real models, recorded in the build's record, and in nothing customers get (served db, public export).
    import pyarrow.parquet as pq

    from coffeedata import control
    from coffeedata.lake import SCHEMAS

    ops = quality.ops_files(lake, build_id)
    assert set(ops) == set(quality.OPS_TABLES)
    for t, f in ops.items():
        assert pq.read_schema(f).remove_metadata() == SCHEMAS[t], t
    recorded = dict(con.execute(
        f"select unique_id, rows from {lake.scan('builds')} where build_id = ? and kind = 'ops_table'", [build_id]
    ).fetchall())  # fmt: skip
    assert recorded == {t: pq.read_metadata(f).num_rows for t, f in ops.items()}
    assert recorded["run_completeness"] == 5 and recorded["product_decisions"] > recorded["run_completeness"]
    decisions = duckdb.sql(
        f"select is_coffee, classified_by, reason from read_parquet('{ops['product_decisions']}')"
    )
    rows = decisions.fetchall()
    assert {c for c, _, _ in rows} == {True, False} and all(
        r for _, _, r in rows
    )  # the non-coffees are there
    served = duckdb.connect(str(cli.SERVED_DB), read_only=True)
    names = {t for (t,) in served.execute("select table_name from information_schema.tables").fetchall()}
    served.close()
    assert names == set(quality.RELEASE_TABLES)
    assert not {p.stem for p in (tmp_path / "public").rglob("*.parquet")} & set(quality.OPS_TABLES)
    # The build's start and end rows in the control plane, with the commit that ran.
    [job] = [r for r in control.rows(lake, week) if r["job"] == "build"]
    assert job["status"] == "done" and job["code_version"]
    assert control.detail_of(job)["build_id"] == build_id and control.detail_of(job)["passed"] is True


def test_the_contracts_are_what_make_contracts_writes_and_valid():
    for name in sorted(raw_contracts.CONTRACTS):
        text = raw_contracts.path(name).read_text()
        assert raw_contracts.render(raw_contracts.generate(name, yaml.safe_load(text)["version"])) == text, (
            f"contracts/internal/{name}.odcs.yaml is stale: run make contracts"
        )
    odcs, odps = _validator("odcs-json-schema-v3.2.0.json"), _validator("odps-json-schema-v1.1.0.json")
    for path in [
        CONTRACTS / "public-catalog.odcs.yaml",
        *sorted((CONTRACTS / "internal").glob("*.odcs.yaml")),
    ]:
        assert [e.message for e in odcs.iter_errors(yaml.safe_load(path.read_text()))] == [], path.name
    assert [
        e.message for e in odps.iter_errors(yaml.safe_load((CONTRACTS / "coffee-data.odps.yaml").read_text()))
    ] == []


def test_a_missing_dbt_fails_the_smoke_job_instead_of_skipping(tmp_path):
    """The review's case: dbt not on PATH. Under REQUIRE_DBT (CI's smoke job) the dbt and web parts fail; without it
    they skip, as on a laptop without dbt."""
    path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "dbt").exists())
    tests = [str(ROOT / "tests/test_smoke_dbt.py") + "::test_the_fixture_lake_builds_passes_the_gate_and_is_served",
             str(ROOT / "tests/test_smoke_web.py")]  # fmt: skip

    def run(**env):
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "t"),
             *tests],
            cwd=ROOT, env={k: v for k, v in os.environ.items() if not k.startswith("REQUIRE_")} | {"PATH": path} | env,
            capture_output=True, text=True,
        )  # fmt: skip

    required = run(REQUIRE_DBT="1")
    assert required.returncode == 1 and "2 failed" in required.stdout, required.stdout[-2000:]
    assert required.stdout.count("REQUIRE_DBT is set, but dbt isn't installed") >= 2
    assert "2 skipped" in run().stdout
