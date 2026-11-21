"""The classifier evaluation's labels live in a private dev dataset that nothing in the pipeline may read.

Two guards (eval/classifier_inputs/README.md):
- no tracked file outside eval/ names that dataset, so no config, workflow, dbt source or code can be pointed at it;
- the scoring script refuses any remote dataset whose name doesn't end in -dev, so it can't be run against the lake
  or the public export.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
# Written in two parts so this file doesn't name the dataset itself.
DATASET = "coffee-eval-" + "labels-dev"


def _tracked() -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [ROOT / p for p in out.decode().split("\0") if p]


def test_nothing_outside_eval_names_the_dev_labels_dataset():
    named = []
    for path in _tracked():
        if path.relative_to(ROOT).parts[0] == "eval" or not path.is_file():
            continue
        if DATASET.encode() in path.read_bytes():
            named.append(str(path.relative_to(ROOT)))
    assert not named, f"{DATASET} is dev data and must not be read by the pipeline; named in: {named}"


def _score():
    spec = importlib.util.spec_from_file_location("score", ROOT / "eval" / "classifier_inputs" / "score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "path",
    [
        "hf://datasets/someone/coffee-data",
        "hf://datasets/someone/us-coffee-market",
        "hf://datasets/someone/labels-dev-copy",
        "hf://datasets/someone/labels-dev/extra",
        "hf://datasets/labels-dev",
        "s3://bucket/labels-dev",
        "https://huggingface.co/datasets/someone/labels-dev",
    ],
)
def test_scoring_refuses_a_dataset_that_is_not_dev(path, monkeypatch):
    def no_download(*args, **kwargs):
        raise AssertionError("a refused dataset must never be fetched")

    monkeypatch.setattr("huggingface_hub.snapshot_download", no_download)
    with pytest.raises(SystemExit, match="refused"):
        _score().data_dir(path)


def test_scoring_takes_a_local_folder_as_it_is(tmp_path):
    assert _score().data_dir(str(tmp_path)) == tmp_path
