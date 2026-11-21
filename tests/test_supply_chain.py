"""Review 2026-11-21, F4, F9, F14: the images' code isn't the runtime user's, hook repositories are pinned by
commit, Dependabot covers the browser tests' npm lockfile, and a pull request's plan needs the plan environment."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


def _lines(path: str) -> list[str]:
    return [
        ln.strip()
        for ln in (ROOT / path).read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_the_images_code_belongs_to_root_and_only_scratch_folders_to_the_user():
    for dockerfile, scratch in (
        ("app/Dockerfile", {"build", ".tmp"}),
        ("ingest.Dockerfile", {"build", "dbt/target", "dbt/logs", ".tmp"}),
    ):
        lines = _lines(dockerfile)
        assert not [ln for ln in lines if "--chown" in ln], dockerfile
        user_at = lines.index("USER user")
        assert user_at > max(i for i, ln in enumerate(lines) if ln.startswith(("COPY", "RUN uv sync")))
        assert not [ln for ln in lines[user_at + 1 :] if ln.startswith(("COPY", "RUN"))], dockerfile
        chown = next(ln for ln in lines if "chown user:user" in ln)
        assert set(chown.split("chown user:user", 1)[1].split()) == scratch, dockerfile
        assert "PYTHONNOUSERSITE=1" in (ROOT / dockerfile).read_text()


def test_hook_repositories_are_pinned_by_commit():
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    config = yaml.safe_load(text)
    remote = [r for r in config["repos"] if r["repo"] != "local"]
    assert remote
    for r in remote:
        assert re.fullmatch(r"[0-9a-f]{40}", r["rev"]), r["repo"]
        assert re.search(rf"rev: {r['rev']}  # frozen: \S+", text), r["repo"]


def test_dependabot_covers_the_browser_tests_npm_lockfile():
    assert (ROOT / "tests/browser/package-lock.json").exists()
    updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
    assert any(u["package-ecosystem"] == "npm" and u["directory"] == "/tests/browser" for u in updates)


def test_a_pull_requests_plan_needs_the_plan_environment():
    main = (ROOT / "src/terraform/bootstrap/main.tf").read_text()
    variables = (ROOT / "src/terraform/bootstrap/variables.tf").read_text()
    pr_arm = next(
        ln
        for ln in main.splitlines()
        if "assertion.workflow_ref.startsWith" in ln and "terraform-plan.yml@refs/pull/" in ln
    )
    assert "assertion.environment == '${var.plan_environment}'" in pr_arm
    default = re.search(r'variable "plan_environment" \{.*?default\s*=\s*"([^"]+)"', variables, re.S).group(1)
    workflow = yaml.safe_load((ROOT / ".github/workflows/terraform-plan.yml").read_text())
    plan = workflow["jobs"]["plan"]
    assert plan["environment"] == default
    assert plan["permissions"].get("id-token") == "write"
    # the only job that can mint the plan identity's token is the one in the environment
    for name, job in workflow["jobs"].items():
        if (job.get("permissions") or {}).get("id-token") == "write":
            assert job.get("environment") == default, name


def test_the_code_lives_in_opt_and_the_users_home_stays_the_users():
    """Review of #250: under /home/user the user could rename the code's folder and put its own in its place; and a
    root `uv sync` there left ~/.cache root's, which broke Hugging Face's cache (hf_hub_download) at runtime. Docker
    can't build here, so the Dockerfiles' paths and ownership are checked as text."""
    for dockerfile, workdir in (("app/Dockerfile", "/opt/app"), ("ingest.Dockerfile", "/opt/ingest")):
        text = (ROOT / dockerfile).read_text()
        lines = _lines(dockerfile)
        assert f"WORKDIR {workdir}" in lines, dockerfile
        assert "/home/user/app" not in text and "/home/user/ingest" not in text, dockerfile
        assert f"PATH={workdir}/.venv/bin:$PATH" in text, dockerfile
        assert "HOME=/home/user" in text and "HF_HOME=/home/user/.cache/huggingface" in text, dockerfile
        sync = [ln for ln in lines if ln.startswith("RUN uv sync")]
        assert sync and all("--no-cache" in ln for ln in sync), dockerfile
        # what root writes under the user's home at build time: only DuckDB's extensions, before USER user
        user_at = lines.index("USER user")
        root_runs = [ln for ln in lines[:user_at] if ln.startswith("RUN")]
        assert not [ln for ln in root_runs if "/home/user" in ln], dockerfile
        assert any("INSTALL httpfs" in ln for ln in root_runs), dockerfile
    assert "COFFEEDATA_STAGING=/opt/app/build/lake-staging" in (ROOT / "app/Dockerfile").read_text()
    # an s3:// export (public._configure_object_store) can't install `aws` at runtime into root's ~/.duckdb
    assert "INSTALL aws" in (ROOT / "ingest.Dockerfile").read_text()
    assert "INSTALL aws" in (ROOT / "src/coffeedata/public.py").read_text()
