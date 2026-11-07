"""Where a lake record came from: the code version, the CI run, the person and the package versions.

Operational metadata only. It is written to raw tables and build records (fetches, builds, serving_log) and never
reaches a served mart or a public table.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import subprocess
from importlib import metadata
from pathlib import Path

# The packages whose versions decide what a build produces.
KEY_PACKAGES = ("coffeedata", "dbt-core", "dbt-duckdb", "duckdb", "pyarrow", "pydantic", "httpx")


# The commit an image was built from, set by its build (ingest.Dockerfile and app/Dockerfile: ARG GIT_SHA, from
# `--build-arg GIT_SHA=$(git rev-parse HEAD)` in Cloud Build and the deploy workflow). An image has no git checkout,
# so without it a Cloud Run build recorded no commit at all.
IMAGE_SHA_ENV = "COFFEEDATA_GIT_SHA"
UNKNOWN = "unknown"


def git_sha() -> str | None:
    """GITHUB_SHA, else the commit the image was built from (COFFEEDATA_GIT_SHA), else the sha of the checkout this
    package runs from (never the cwd's repo), else None. An image built without the commit has
    COFFEEDATA_GIT_SHA=unknown, which counts as missing."""
    for var in ("GITHUB_SHA", IMAGE_SHA_ENV):
        if (v := os.environ.get(var, "").strip()) and v != UNKNOWN:
            return v
    try:
        here = Path(__file__).resolve().parent
        r = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return (r.stdout or "").strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def code_version() -> str:
    """The commit the code is (git_sha), else "unknown": never just the package's version, which every commit
    shares."""
    return git_sha() or UNKNOWN


def package_versions() -> str:
    """JSON object of Python's and the key packages' versions (a package that isn't installed is left out)."""
    found = {p: v for p in KEY_PACKAGES if (v := package_version(p))}
    return json.dumps({"python": platform.python_version()} | found, sort_keys=True)


def ci_run() -> tuple[str | None, str | None]:
    """The GitHub Actions run id and its URL, or (None, None) outside Actions."""
    run_id = os.environ.get("GITHUB_RUN_ID") or None
    server, repo = os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_REPOSITORY")
    url = f"{server.rstrip('/')}/{repo}/actions/runs/{run_id}" if run_id and server and repo else None
    return run_id, url


def actor() -> str | None:
    """Who did it: GITHUB_ACTOR in Actions, else the local user."""
    if os.environ.get("GITHUB_ACTOR"):
        return os.environ["GITHUB_ACTOR"]
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return None
