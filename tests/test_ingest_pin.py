"""RUNBOOK §4's pinning block and docs/ingestion/verified-ingest-image.sh fail closed: with stand-in `gcloud`, `gh`
and `git`, anything short of a found, well-formed, verified digest pins nothing, even when the lines are pasted
into an interactive shell that carries on after an error and already holds an older $PINNED."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SHA = "1" * 40
DIGEST = "sha256:" + "a" * 64
REPO = "us-central1-docker.pkg.dev/example-project/registry-coffee/ingest"
STALE = f"{REPO}@sha256:{'b' * 64}"  # what an earlier paste left in the shell

STUBS = {
    "git": f"""case "$*" in
  "rev-parse HEAD") echo {SHA} ;;
  "rev-parse --short=12 HEAD") echo {SHA[:12]} ;;
esac""",
    "gcloud": """echo "gcloud $*" >> "$CALLS"
case "$*" in
  *"images describe"*) [ -n "${DESCRIBE_FAILS:-}" ] && { echo "NOT_FOUND" >&2; exit 1; }; echo "$DESCRIBE_PRINTS" ;;
esac""",
    "gh": """echo "gh $*" >> "$CALLS"
[ -z "${VERIFY_FAILS:-}" ]""",
}

FAILURES = {
    "lookup empty": {"DESCRIBE_PRINTS": ""},
    "lookup error": {"DESCRIBE_FAILS": "1"},
    "malformed digest": {"DESCRIBE_PRINTS": "sha256:abc"},
    "a tag, not a digest": {"DESCRIBE_PRINTS": "latest"},
    "attestation fails": {"VERIFY_FAILS": "1"},
}


def pin_block() -> str:
    text = (ROOT / "docs/ingestion/RUNBOOK.md").read_text()
    block = re.search(r"# pin-block: begin[^\n]*\n(.*?)# pin-block: end", text, re.S).group(1)
    return "\n".join(line.strip() for line in block.splitlines()) + "\n"


def run(tmp_path, block: str, interactive: bool, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        path = bin_dir / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls"
    calls.write_text("")
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "CALLS": str(calls),
        "REGION": "us-central1",
        "IMAGE": f"{REPO}:latest",
        "PINNED": STALE,
        "DESCRIBE_PRINTS": DIGEST,
        **env,
    }
    # An interactive shell doesn't exit at `${VAR:?}` or a failed command: it runs the next pasted line.
    script = block + 'echo "after: PINNED=${PINNED-}"\n'
    command = ["bash", "--norc", "--noprofile", "-i"] if interactive else ["bash"]
    done = subprocess.run(
        command, input=script, capture_output=True, text=True, cwd=ROOT, env=full_env, timeout=60
    )
    return done, calls.read_text()


def test_the_block_in_the_runbook_is_the_one_tested():
    block = pin_block()
    assert "unset PINNED" in block.splitlines()[0]
    assert "verified-ingest-image.sh" in block and "tags add" in block
    runbook = (ROOT / "docs/ingestion/RUNBOOK.md").read_text()
    assert '|| echo "STOP' not in runbook  # a check that prints and carries on
    assert 'terraform plan -var ingest_image="${PINNED:?' in runbook and "rm -f ingest.plan" in runbook


@pytest.mark.parametrize("interactive", [False, True])
def test_a_verified_image_is_printed_and_tagged(tmp_path, interactive):
    done, calls = run(tmp_path, pin_block(), interactive)
    assert f"pin: {REPO}@{DIGEST}" in done.stdout
    assert f"after: PINNED={REPO}@{DIGEST}" in done.stdout
    assert f"tags add {REPO}@{DIGEST} {REPO}:pinned-{SHA[:12]}" in calls
    assert f"--source-digest {SHA}" in calls and "--source-ref refs/heads/main" in calls


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("failure", FAILURES)
def test_any_failure_pins_nothing(tmp_path, failure, interactive):
    done, calls = run(tmp_path, pin_block(), interactive, **FAILURES[failure])
    assert "tags add" not in calls
    assert (
        "pin: " not in done.stdout and "sha256:" not in done.stdout
    )  # nothing to copy, the stale value included
    if interactive:
        assert "after: PINNED=\n" in done.stdout  # the shell carried on, with nothing in $PINNED
    else:
        assert done.returncode != 0 and "after:" not in done.stdout


@pytest.mark.parametrize("failure", FAILURES)
def test_the_script_exits_nonzero_and_prints_nothing(tmp_path, failure):
    script = f'docs/ingestion/verified-ingest-image.sh "{REPO}"\n'
    done, _ = run(tmp_path, script, False, **FAILURES[failure])
    assert "STOP" in done.stderr
    assert not [line for line in done.stdout.splitlines() if not line.startswith("after:")]
    done, _ = run(tmp_path, script + "exit $?\n", False, **FAILURES[failure])
    assert done.returncode != 0


def test_the_script_refuses_a_tagged_image_name_and_a_short_commit(tmp_path):
    for args in (f'"{REPO}:latest"', f'"{REPO}" abc123'):
        done, calls = run(tmp_path, f"docs/ingestion/verified-ingest-image.sh {args}; exit $?\n", False)
        assert done.returncode != 0 and "sha256:" not in done.stdout and "gh " not in calls
