"""Smoke, dbt (`make smoke`; CI's smoke job, part dbt). The recorded storefronts as a weekly run, through the path
`coffeedata build` takes: every model, then every gate test, the build's record and release, then serving it. Every
public_* table has rows, the public export records each file's sha256, and the contracts are what
`make contracts` writes and valid ODCS/ODPS."""

import json
import shutil

import duckdb
import pytest
import yaml

from coffeedata import cli, connectors, public, quality, raw_contracts

from .smoke import fixture_lake
from .test_contracts import CONTRACTS, _validator

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed"),
]


def test_the_fixture_lake_builds_passes_the_gate_and_is_served(lake, tmp_path, monkeypatch, no_network):
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
            "select table_name from information_schema.tables where table_name like 'public_%'"
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
