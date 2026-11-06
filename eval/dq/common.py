"""Shared pieces of the data-quality rule evaluations: the split, the reference labels, Wilson intervals."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path

SALT = "coffeedata-dq-eval-2026-11-04"
SETS = ("train", "validation", "test")


def bucket(key: str, salt: str = SALT) -> int:
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()[:8], 16) % 100


def split_of(group_key: str) -> str:
    """60/20/20 by a salted hash of the group key (a roaster_id, or for a roaster pair its cluster's first id)."""
    n = bucket(group_key)
    return "train" if n < 60 else "validation" if n < 80 else "test"


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def fmt(k: float, n: float) -> str:
    if n <= 0:
        return "n/a (0)"
    lo, hi = wilson(k, n)
    return f"{k / n:.3f} ({k:g}/{n:g}; {lo:.3f}-{hi:.3f})"


def _label_file(labels_dir: Path, rule: str, labeler: str) -> Path:
    """labels/<rule>_<X>.json holds train and validation; the sealed test copy is sealed/<rule>_<X>_test.json."""
    plain = labels_dir / f"{rule}_{labeler}.json"
    return plain if plain.exists() else labels_dir / f"{rule}_{labeler}_test.json"


def reference(labels_dir: Path, rule: str) -> dict[str, str]:
    """A and B's labels where they agree, C's where they don't (eval/dq/README.md, "Reference labels"). Score train
    and validation from labels/, the test set from sealed/ (its files are covered by sealed/SHA256SUMS)."""
    a, b = (_labels(_label_file(labels_dir, rule, x)) for x in "AB")
    c_path = _label_file(labels_dir, rule, "C")
    c = _labels(c_path) if c_path.exists() else {}
    if set(a) != set(b):
        raise SystemExit(f"{rule}: labelers A and B labelled different items")
    out = {}
    for k in a:
        if a[k] == b[k]:
            out[k] = a[k]
        elif k in c:
            out[k] = c[k]
        else:
            raise SystemExit(f"{rule}: {k} is a disagreement with no settling label")
    return out


def agreement(labels_dir: Path, rule: str) -> str:
    a, b = (_labels(_label_file(labels_dir, rule, x)) for x in "AB")
    same = sum(a[k] == b[k] for k in a)
    pairs = Counter((a[k], b[k]) for k in a if a[k] != b[k])
    return f"A/B agree on {same} of {len(a)}; disagreements {dict(pairs)}"


def _labels(path: Path) -> dict[str, str]:
    return {x["id"]: x["label"] for x in json.loads(path.read_text())["labels"]}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
