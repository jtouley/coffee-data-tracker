"""eval/floor_inputs.py, which the eval-floors workflow runs with a Hub token: it only reads the lake, at the
pinned revisions, and retries only what a retry can fix."""

import sys
from pathlib import Path

import huggingface_hub
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))
import floor_inputs  # noqa: E402

WRITES = ["create_repo", "upload_file", "upload_folder", "create_commit", "delete_file", "delete_folder",
          "delete_repo", "update_repo_settings", "create_branch", "super_squash_history"]  # fmt: skip


@pytest.fixture
def no_hub_writes(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("eval/floor_inputs.py wrote to the Hub")

    for name in WRITES:
        monkeypatch.setattr(huggingface_hub, name, refuse, raising=False)
        monkeypatch.setattr(huggingface_hub.HfApi, name, refuse, raising=False)


def test_it_reads_only_the_pinned_lake_revisions(tmp_path, no_hub_writes):
    calls = []

    def get(**kw):
        calls.append(kw)
        raise RuntimeError("stop after the first download")

    with pytest.raises(RuntimeError, match="stop"):
        floor_inputs.build(tmp_path, get=get)
    (call,) = calls  # a non-transient error isn't retried
    assert call["repo_id"] == "jtouley/coffee-data" and call["repo_type"] == "dataset"
    assert (
        call["revision"] in floor_inputs.DOWNLOADS and len(call["revision"]) == 40
    )  # a commit, never a branch
    assert call["allow_patterns"] == floor_inputs.DOWNLOADS[call["revision"]]
    assert str(tmp_path) in call["local_dir"]
    # nothing in the script names a write
    src = Path(floor_inputs.__file__).read_text()
    assert not [w for w in WRITES if w in src]


def _http_error(status: int):
    """An HfHubHTTPError as huggingface_hub 2.x raises it: over an httpx2 response."""
    import httpx2
    from huggingface_hub.errors import HfHubHTTPError

    resp = httpx2.Response(status, request=httpx2.Request("GET", "https://huggingface.co/api/datasets/x"))
    return HfHubHTTPError(f"{status}", response=resp)


def _wrapped(cause: BaseException):
    """What snapshot_download raises when listing the revision failed and nothing is cached: a
    LocalEntryNotFoundError from the error it caught."""
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        raise LocalEntryNotFoundError("cannot find the requested files") from cause
    except LocalEntryNotFoundError as e:
        return e


def test_transient_errors_are_retried_with_jittered_backoff_and_permanent_ones_are_not(tmp_path):
    import httpx2
    from huggingface_hub.errors import OfflineModeIsEnabled, RevisionNotFoundError

    req = httpx2.Request("GET", "https://huggingface.co/api/datasets/x")
    transient = [
        httpx2.ConnectError("refused", request=req),
        httpx2.ReadTimeout("slow", request=req),
        _http_error(503),
        _http_error(429),
        _wrapped(httpx2.ConnectError("refused", request=req)),
        _wrapped(_http_error(504)),
    ]
    answers = iter([*transient[:3], None])
    waits: list[float] = []

    def flaky(**kw):
        if e := next(answers):
            raise e

    floor_inputs.download(
        floor_inputs.LAKE_1103, tmp_path, get=flaky, sleep=waits.append, jitter=lambda: 0.25
    )
    assert waits == [1.5, 3.0, 6.0]  # 2, 4, 8 s, each scaled by 0.5 + jitter
    assert all(floor_inputs._transient(e) for e in transient)

    gone = RevisionNotFoundError("gone", response=_http_error(404).response)
    offline = OfflineModeIsEnabled("HF_HUB_OFFLINE is set")
    for permanent in (_http_error(401), _http_error(404), gone, _wrapped(_http_error(401)),
                      _wrapped(ValueError("x")), ValueError("a bug"), offline, _wrapped(offline)):  # fmt: skip
        n: list[int] = []

        def refuse(n=n, permanent=permanent, **kw):
            n.append(1)
            raise permanent

        with pytest.raises(type(permanent)):
            floor_inputs.download(floor_inputs.LAKE_1103, tmp_path, get=refuse, sleep=lambda s: None)
        assert len(n) == 1, permanent

    def always(**kw):
        raise httpx2.ConnectError("down", request=req)

    with pytest.raises(httpx2.ConnectError):
        floor_inputs.download(floor_inputs.LAKE_1103, tmp_path, get=always, sleep=lambda s: None)


def test_the_real_snapshot_download_failing_to_connect_is_retried(tmp_path, monkeypatch):
    """Through huggingface_hub's own snapshot_download, against a Hub that refuses connections (a closed local port):
    it raises LocalEntryNotFoundError from httpx2's ConnectError, which is retried, ATTEMPTS times in all."""
    from huggingface_hub import constants
    from huggingface_hub.errors import LocalEntryNotFoundError

    monkeypatch.setattr(constants, "ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)  # the suite may run with HF_HUB_OFFLINE=1
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("HF_TOKEN", "hf_dummy")
    monkeypatch.setattr("time.sleep", lambda s: None)  # huggingface_hub's own retries of each request
    waits: list[float] = []
    with pytest.raises(LocalEntryNotFoundError) as err:
        floor_inputs.download(floor_inputs.LAKE_1103, tmp_path / "out", sleep=waits.append)
    assert type(err.value.__cause__).__module__.startswith("httpx2")
    assert len(waits) == floor_inputs.ATTEMPTS - 1


def test_inputs_already_built_with_the_recorded_checksums_are_not_downloaded(tmp_path, monkeypatch):
    for rel in floor_inputs.SHA256:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(rel)
    monkeypatch.setattr(
        floor_inputs, "SHA256", {rel: floor_inputs.sha256(tmp_path / rel) for rel in floor_inputs.SHA256}
    )
    dirs = floor_inputs.build(tmp_path, get=lambda **kw: pytest.fail("downloaded although cached"))
    assert dirs["ORIGIN_EVAL_DIR"] == tmp_path / "origin"
    (tmp_path / "origin" / "listings.json").write_text("changed")
    with pytest.raises(RuntimeError, match="stop"):
        floor_inputs.build(tmp_path, get=lambda **kw: (_ for _ in ()).throw(RuntimeError("stop")))


def test_a_floor_without_its_inputs_fails_under_require_evals(monkeypatch):
    from .evalfloor import eval_dir

    monkeypatch.delenv("ORIGIN_EVAL_DIR", raising=False)
    monkeypatch.setenv("REQUIRE_EVALS", "1")
    with pytest.raises(pytest.fail.Exception, match="REQUIRE_EVALS=1"):
        eval_dir("ORIGIN_EVAL_DIR")
    monkeypatch.delenv("REQUIRE_EVALS")
    with pytest.raises(pytest.skip.Exception, match="private lake data"):
        eval_dir("ORIGIN_EVAL_DIR")


def test_no_hub_token_reaches_pull_request_code_and_the_floors_never_gate_deploys():
    """The eval-floors workflow runs on main, weekly and by hand, never on a pull request; no workflow a pull request
    triggers names a Hub token; and deploy.yml waits on ci, which doesn't run the private floors."""
    import yaml

    workflows = Path(__file__).parent.parent / ".github" / "workflows"
    for path in workflows.glob("*.yml"):
        text = path.read_text()
        triggers = yaml.safe_load(text)[True]  # YAML 1.1 reads the `on:` key as True
        triggers = set(triggers) if isinstance(triggers, dict | list) else {triggers}
        if triggers & {"pull_request", "pull_request_target"}:
            assert "HF_TOKEN" not in text and "HF_READ_TOKEN" not in text, path.name
    floors = yaml.safe_load((workflows / "eval-floors.yml").read_text())
    assert set(floors[True]) == {"push", "schedule", "workflow_dispatch"}
    assert floors[True]["push"] == {"branches": ["main"]}
    # a dispatch can name any branch: every job holding the token runs on main only
    for name, job in floors["jobs"].items():
        assert job.get("if") == "github.ref == 'refs/heads/main'", name
    assert "private_eval" not in (workflows / "ci.yml").read_text()
    assert yaml.safe_load((workflows / "deploy.yml").read_text())[True]["workflow_run"]["workflows"] == ["ci"]
