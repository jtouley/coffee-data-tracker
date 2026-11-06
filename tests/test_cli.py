"""The weekly run's order of steps."""

import argparse

import pytest

from coffeedata import cli


def test_weekly_publishes_raw_data_before_build(monkeypatch):
    """A build, export or Snowflake failure must not lose the week's fetched or enriched data."""
    calls = []
    for name in (
        "cmd_probe",
        "cmd_ingest",
        "cmd_enrich",
        "cmd_build",
        "cmd_publish",
        "cmd_export_public",
        "cmd_load_snowflake",
        "cmd_site",
    ):
        monkeypatch.setattr(cli, name, lambda a, n=name: calls.append(n))
    monkeypatch.delenv("PUBLIC_EXPORT_URL", raising=False)
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    cli.cmd_weekly(
        argparse.Namespace(
            run_date="2026-11-09",
            discover=False,
            sample=None,
            enrich_limit=None,
            db="build/coffee.duckdb",
            out="build/site",
        )
    )
    for step in ("cmd_probe", "cmd_ingest", "cmd_enrich", "cmd_build"):
        i = max(i for i, c in enumerate(calls) if c == step)
        assert calls[i + 1] == "cmd_publish", f"{step} is not followed by a publish: {calls}"
    assert calls.index("cmd_export_public") > max(i for i, c in enumerate(calls) if c == "cmd_publish")


def test_weekly_runs_to_the_end_and_publishes_only_a_passing_build(monkeypatch):
    """A build that fails its data checks is still recorded and published as a record, nothing derived is
    published (no export, no site), and the run exits non-zero so the job shows it."""
    calls = []
    for name in ("cmd_probe", "cmd_ingest", "cmd_enrich", "cmd_classify", "cmd_export_public", "cmd_site"):
        monkeypatch.setattr(cli, name, lambda a, n=name: calls.append(n))
    monkeypatch.setattr(cli, "cmd_publish", lambda a: calls.append(a.message))
    monkeypatch.setattr(cli, "cmd_build", lambda a: calls.append("cmd_build") or False)
    ns = argparse.Namespace(run_date="2026-11-09", discover=False, sample=None, enrich_limit=None,
                            db="build/coffee.duckdb", out="build/site", accept_volume_change=False)  # fmt: skip
    with pytest.raises(SystemExit, match="failed its data checks"):
        cli.cmd_weekly(ns)
    assert calls[calls.index("cmd_build") + 1] == "weekly run 2026-11-09: failed build record"
    assert "cmd_export_public" not in calls and "cmd_site" not in calls


def _claims_passed_to_dbt(monkeypatch, tmp_path) -> list[dict]:
    """Run dbt_build with dbt itself faked, and return the coverage claims it passed as vars."""
    import json
    import subprocess

    from coffeedata.lake import Lake

    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(cli, "DBT_TARGET", tmp_path / "target")
    argv = []
    monkeypatch.setattr(
        cli.subprocess, "run", lambda a, **kw: argv.append(a) or subprocess.CompletedProcess(a, 0)
    )
    passed, _ = cli.dbt_build(Lake(), tmp_path / "coffee.duckdb")
    # Both gate steps ran, then the report-only step (release_changes) after them.
    assert passed and len(argv) == 3 and argv[2][argv[2].index("--select") + 1] == "tag:report_only+"
    return json.loads(argv[0][argv[0].index("--vars") + 1])["coverage_claims"]


def test_a_listing_claim_that_does_not_parse_is_skipped_in_the_build_not_raised(
    monkeypatch, tmp_path, caplog
):
    from coffeedata import quality

    listing = tmp_path / "LISTING.md"
    listing.write_text(
        "<!-- coverage-claims:\nlisted_coffees: about 50,000 coffees\nroasters_with_coffee: roasters\n"
        "roasters_with_coffee: 1,800 roasters\n-->\nAbout 45,000 coffees from 1,800 roasters.\n"
    )
    monkeypatch.setattr(quality, "LISTING", listing)
    claims = _claims_passed_to_dbt(monkeypatch, tmp_path)
    assert [(c["metric"], c["value"]) for c in claims] == [("roasters_with_coffee", 1800.0)]
    assert "'about 50,000 coffees' is not in the copy; not checked" in caplog.text
    assert "'roasters' has no number; not checked" in caplog.text
    with pytest.raises(ValueError, match="is not in the copy"):  # CI's parse test stays strict
        quality.coverage_claims(listing)


def test_a_missing_listing_checks_no_claims_and_warns(monkeypatch, tmp_path, caplog):
    from coffeedata import quality

    monkeypatch.setattr(quality, "LISTING", tmp_path / "marketplace" / "LISTING.md")
    assert _claims_passed_to_dbt(monkeypatch, tmp_path) == []
    assert "coverage claims not checked" in caplog.text and "can't be read" in caplog.text
    with pytest.raises(FileNotFoundError):
        quality.coverage_claims()


def test_the_listing_is_found_from_the_package_not_the_working_directory(monkeypatch, tmp_path):
    from coffeedata import quality

    monkeypatch.chdir(tmp_path)  # no marketplace/ here
    assert {c["metric"] for c in quality.coverage_claims()} == {"listed_coffees", "roasters_with_coffee"}


def test_the_ingest_image_has_the_listing_where_the_package_looks_for_it():
    """The build job's image copies src/ and marketplace/LISTING.md side by side under its WORKDIR, so
    quality.LISTING (the package checkout's marketplace/LISTING.md) exists there, and the build context keeps it."""
    import fnmatch

    from coffeedata import quality

    dockerfile = (quality.ROOT / "ingest.Dockerfile").read_text().splitlines()
    assert "COPY --chown=user src ./src" in dockerfile
    assert "COPY --chown=user marketplace/LISTING.md ./marketplace/LISTING.md" in dockerfile
    assert quality.LISTING.relative_to(quality.ROOT).as_posix() == "marketplace/LISTING.md"

    def kept(path: str) -> bool:  # Docker's rule: the last matching pattern wins, and `!` re-includes
        keep = True
        for raw in (quality.ROOT / "ingest.Dockerfile.dockerignore").read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate, pattern = line.startswith("!"), line.lstrip("!")
            parts = path.split("/")
            if any(fnmatch.fnmatch("/".join(parts[:i]), pattern) for i in range(1, len(parts) + 1)):
                keep = negate
        return keep

    assert kept("marketplace/LISTING.md") and kept("src/coffeedata/quality.py")
    assert not kept("marketplace/README.md")  # the rest of marketplace/ stays out


def test_a_failure_reporting_the_coverage_claims_never_stops_the_build_record(monkeypatch, tmp_path, caplog):
    import duckdb

    from coffeedata import quality

    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(quality, "served", lambda lk: None)
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: (True, [{}, {}]))

    def broken(db):
        raise duckdb.IOException("a partial coffee.duckdb")

    monkeypatch.setattr(quality, "coverage_claim_messages", broken)
    recorded = []
    monkeypatch.setattr(quality, "record_build", lambda *a: recorded.append(a[2]) or False)
    monkeypatch.setattr(cli, "_write_personal_marts", lambda *a: None)
    args = argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date="2026-10-26")
    assert cli.cmd_build(args) is False  # record_build's verdict, so the build was recorded
    assert recorded == ["2026-10-26"]
    assert "coverage claims not reported: a partial coffee.duckdb" in caplog.text
