"""Where an accuracy floor finds its private inputs (eval/floor_inputs.py rebuilds them from the lake, read-only).

Locally, a floor whose folder isn't set is skipped, saying why. CI's eval-floors job sets REQUIRE_EVALS=1 once it has
built the inputs, so there a missing folder fails the floor instead of skipping it.
"""

import os
from pathlib import Path

import pytest


def eval_dir(var: str) -> Path:
    path = os.environ.get(var)
    if path:
        return Path(path)
    why = f"{var} not set: the floor's inputs are private lake data (build them with eval/floor_inputs.py)"
    if os.environ.get("REQUIRE_EVALS") == "1":
        pytest.fail(f"{why}; REQUIRE_EVALS=1 forbids skipping")
    pytest.skip(why)
