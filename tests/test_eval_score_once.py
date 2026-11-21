"""The classifier evaluation's sealed sets (test, test_next) are scored once each (eval/classifier_inputs/README.md).

score.py refuses `score` on a sealed set, and refuses `final` on a set that has a FINAL_SCORED marker or whose
sample.json hash is in the tracked FINAL_SCORED record, so a fresh download or another machine can't score it again.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HERE = ROOT / "eval" / "classifier_inputs"


def _score():
    spec = importlib.util.spec_from_file_location("score", HERE / "score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sealed_set(data: Path, name: str, labels: bool = True) -> Path:
    d = data / name
    d.mkdir(parents=True)
    items = [{"id": 1, "stratum": "kept"}, {"id": 2, "stratum": "dropped_other"}]
    (d / "sample.json").write_text(
        json.dumps({"set": name, "population": {"kept": 10, "dropped_other": 20}, "items": items})
    )
    if labels:
        for who in "AB":
            (d / f"labels_{who}.json").write_text(
                json.dumps([{"id": 1, "label": "coffee"}, {"id": 2, "label": "not_coffee"}])
            )
    return d


def _preds(tmp_path: Path) -> str:
    p = tmp_path / "preds.json"
    row = {"rules_only": True, "excluded": False, "by": "rules"}
    p.write_text(json.dumps({"1": row | {"is_coffee": True}, "2": row | {"is_coffee": False}}))
    return f"main={p}"


def _run(module, monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["score.py", *map(str, argv)])
    module.main()


@pytest.fixture
def score(tmp_path, monkeypatch):
    module = _score()
    monkeypatch.setattr(module, "RECORD", tmp_path / "record")
    return module


@pytest.mark.parametrize("name", ["test", "test_next"])
def test_score_refuses_every_sealed_set(name, score, tmp_path, monkeypatch):
    _sealed_set(tmp_path / "data", name)
    with pytest.raises(SystemExit, match="sealed set"):
        _run(score, monkeypatch, "score", tmp_path / "data", name, _preds(tmp_path))


@pytest.mark.parametrize("name", ["train", "validation"])
def test_final_refuses_a_set_that_is_not_sealed(name, score, tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="refused"):
        _run(score, monkeypatch, "final", tmp_path / "data", name, _preds(tmp_path))


def test_final_scores_test_next_exactly_once(score, tmp_path, monkeypatch, capsys):
    d = _sealed_set(tmp_path / "data", "test_next")
    _run(score, monkeypatch, "final", tmp_path / "data", "test_next", _preds(tmp_path))
    assert "accuracy weighted by stratum size 100.00%" in capsys.readouterr().out
    digest = hashlib.sha256((d / "sample.json").read_bytes()).hexdigest()
    assert score.RECORD.read_text().split()[:2] == [digest, "test_next"]
    assert (d / "FINAL_SCORED").read_text() == "main\n"

    with pytest.raises(SystemExit, match="already scored once"):
        _run(
            score,
            monkeypatch,
            "final",
            tmp_path / "data",
            "test_next",
            "other=" + _preds(tmp_path).split("=")[1],
        )
    # A fresh copy without the marker (another machine, a new download) is refused by the tracked record.
    (d / "FINAL_SCORED").unlink()
    with pytest.raises(SystemExit, match="already scored once"):
        _run(score, monkeypatch, "final", tmp_path / "data", "test_next", _preds(tmp_path))


def test_a_failed_final_run_leaves_the_set_unspent(score, tmp_path, monkeypatch, capsys):
    d = _sealed_set(tmp_path / "data", "test_next", labels=False)
    (d / "labels_A.json").write_text(json.dumps([{"id": 1, "label": "coffee"}]))
    (d / "labels_B.json").write_text(json.dumps([{"id": 1, "label": "coffee"}, {"id": 2, "label": "coffee"}]))
    with pytest.raises(SystemExit, match="different products"):
        _run(score, monkeypatch, "final", tmp_path / "data", "test_next", _preds(tmp_path))
    assert not score.RECORD.exists() and not (d / "FINAL_SCORED").exists()
    assert "accuracy" not in capsys.readouterr().out


def test_an_unlabeled_sealed_set_is_refused_and_stays_unspent(score, tmp_path, monkeypatch):
    d = _sealed_set(tmp_path / "data", "test_next", labels=False)
    with pytest.raises(SystemExit, match="no labels"):
        _run(score, monkeypatch, "final", tmp_path / "data", "test_next", _preds(tmp_path))
    assert not score.RECORD.exists() and not (d / "FINAL_SCORED").exists()


def test_the_spent_test_set_is_recorded():
    """The test set scored on 2026-11-20 is in the tracked record, by the hash SHA256SUMS gives its sample."""
    sums = {
        path: digest
        for digest, path in (line.split() for line in (HERE / "SHA256SUMS").read_text().splitlines())
    }
    recorded = {line.split()[0]: line.split()[1] for line in (HERE / "FINAL_SCORED").read_text().splitlines()}
    assert recorded[sums["test/sample.json"]] == "test"
    assert sums["test_next/sample.json"] not in recorded


@pytest.mark.parametrize(
    "preds",
    [
        [],
        ["main"],
        ["=preds.json"],
        ["main=preds.json@no_such_stratum"],
        ["main=missing.json"],
        ["main=preds.json", "main=preds.json"],
    ],
    ids=["none", "no-equals", "no-name", "unknown-stratum", "no-file", "same-name"],
)
def test_final_refuses_bad_predictions_before_scoring(preds, score, tmp_path, monkeypatch, capsys):
    d = _sealed_set(tmp_path / "data", "test_next")
    _preds(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="refused"):
        _run(score, monkeypatch, "final", tmp_path / "data", "test_next", *preds)
    assert not score.RECORD.exists() and not (d / "FINAL_SCORED").exists()
    assert "accuracy" not in capsys.readouterr().out


def test_final_takes_a_known_stratum_after_at(score, tmp_path, monkeypatch, capsys):
    _sealed_set(tmp_path / "data", "test_next")
    _run(score, monkeypatch, "final", tmp_path / "data", "test_next", _preds(tmp_path) + "@kept")
    assert "kept scored on the rules alone" in capsys.readouterr().out
