"""eval/floor_inputs.py, which CI's eval-floors job runs with the repo's HF_TOKEN: it only reads the lake, at the
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


def test_transient_errors_are_retried_with_backoff_and_permanent_ones_are_not(tmp_path):
    import httpx
    from huggingface_hub.errors import HfHubHTTPError, RevisionNotFoundError

    def http_error(status):
        resp = httpx.Response(status, request=httpx.Request("GET", "https://huggingface.co/x"))
        return HfHubHTTPError(f"{status}", response=resp)

    answers = iter([http_error(503), ConnectionError("reset"), None])
    waits: list[float] = []

    def flaky(**kw):
        e = next(answers)
        if e:
            raise e

    floor_inputs.download(floor_inputs.LAKE_1103, tmp_path, get=flaky, sleep=waits.append)
    assert waits == [2.0, 4.0]

    gone = RevisionNotFoundError(
        "gone", response=httpx.Response(404, request=httpx.Request("GET", "https://x"))
    )
    for permanent in (http_error(401), http_error(404), gone, ValueError("a bug")):
        n = []

        def refuse(n=n, permanent=permanent, **kw):
            n.append(1)
            raise permanent

        with pytest.raises(type(permanent)):
            floor_inputs.download(floor_inputs.LAKE_1103, tmp_path, get=refuse, sleep=lambda s: None)
        assert len(n) == 1, permanent

    def always(**kw):
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        floor_inputs.download(floor_inputs.LAKE_1103, tmp_path, get=always, sleep=lambda s: None)


def test_a_floor_without_its_inputs_fails_under_require_evals(monkeypatch):
    from .evalfloor import eval_dir

    monkeypatch.delenv("ORIGIN_EVAL_DIR", raising=False)
    monkeypatch.setenv("REQUIRE_EVALS", "1")
    with pytest.raises(pytest.fail.Exception, match="REQUIRE_EVALS=1"):
        eval_dir("ORIGIN_EVAL_DIR")
    monkeypatch.delenv("REQUIRE_EVALS")
    with pytest.raises(pytest.skip.Exception, match="private lake data"):
        eval_dir("ORIGIN_EVAL_DIR")
