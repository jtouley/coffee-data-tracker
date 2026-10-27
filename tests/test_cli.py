"""The weekly run's order of steps."""

import argparse

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
