"""The importers workflow's build alert (docs/TRIAGE.md): one triage issue, `pipeline:importer-build`, open while the
private importer build that follows each weekly snapshot fails, closed by the next build that passes.

    python -m coffeedata.importer_build_alert <success|failure> [--dry-run]

It runs in its own job of .github/workflows/importers.yml, after the build job, with GITHUB_TOKEN (issues: write) and
never the Hub token, and is told only the build job's result: it reads nothing from the build, so the issue carries a
status and the run's link, never a lot. It uses triage's issue handling as it is (one issue per key, updated in place,
reopened, closed on recovery), labelled `importers` like the importers' weekly alert, and only the standard library,
so the job installs nothing.

A failed build never touches the raw snapshot: the snapshot job has finished and stored its weeks by then, and the
derived copy in the dataset stays the last one that passed.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from . import triage

KEY = "pipeline:importer-build"
LABEL = "importers"
# the same colour and description the importers' weekly alert gives this label (importers-alerts)
LABEL_SPEC = ("c5def5", "A weekly importer snapshot failed, didn't run, or is stale")
SOURCE = ".github/workflows/importers.yml"
STEPS = [
    "Open the run and see which step of the `build` job failed. Its log holds counts only (dbt's nodes by status): "
    "dbt's own output stays on the runner, since an error can quote a lot's value. The raw snapshot is not affected: "
    "the snapshot job stored its weeks before the build began, and the derived copy in the dataset is the last one "
    "that passed.",
    "Reproduce locally, with read access to the private importers dataset: `COFFEEDATA_IMPORTERS=<its hf:// URL> "
    "uv run coffeedata importer-build` prints dbt's output on your own console. Keep values out of this issue and "
    "its comments: give the failing model or test and the kind of error only.",
    "Fix the cause in main. The next weekly run builds again, and this issue closes when a build passes. To build "
    "sooner, dispatch `gh workflow run importers` (a dispatch never fetches Royal, and a week already stored is "
    "never fetched again).",
]


def build_alert(result: str, run_url: str | None, sha: str | None) -> dict[str, Any]:
    """The alert for the build job's result: firing on `failure` only (a cancelled or skipped build judges nothing,
    so the caller doesn't run)."""
    ok = result == "success"
    return {
        "key": KEY,
        "kind": "pipeline",
        "label": LABEL,
        "title": "importer-build: the derived importer offers weren't rebuilt",
        "severity": "warning",
        "firing": not ok,
        "source": SOURCE,
        "summary": "The private importer build after the weekly snapshot failed; the raw weeks are stored, and the "
        "derived copy in the dataset is the last one that passed.",
        "checks": [{"name": "importer-build", "status": "ok" if ok else "failed"}],
        "context": {"run": run_url, "commit": sha},
        "steps": STEPS,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m coffeedata.importer_build_alert", description=__doc__.split("\n")[0]
    )
    p.add_argument("result", choices=("success", "failure"), help="the build job's result")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    server, repo, run_id = (
        os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None
    alert = build_alert(a.result, run_url, os.environ.get("GITHUB_SHA"))
    token = os.environ.get("GITHUB_TOKEN")
    gh: triage.Client
    if a.dry_run:
        gh = triage.DryRun()
    elif not (token and repo):
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are needed (or --dry-run)", file=sys.stderr)
        return 2
    else:
        gh = triage.GitHub(repo, token)
    if alert["firing"]:
        gh.ensure_label(LABEL, *LABEL_SPEC)  # triage.LABELS has it once the importers' weekly alert is merged
    for action, key, number in triage.sync(
        [alert], gh, author=os.environ.get("TRIAGE_AUTHOR") or triage.AUTHOR
    ):
        print(f"{action} {key} #{number}")
    print(f"importer-build: {a.result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
