"""The accuracy floors' shared plumbing: where a floor finds its private inputs, how it states a check, and the report
the alerts read (docs/TRIAGE.md).

Inputs. Locally, a floor whose folder isn't set is skipped, saying why. The eval-floors workflow sets
REQUIRE_EVALS=1 once it has built the inputs, so there a missing folder fails the floor instead of skipping it.

Checks. A floor states each threshold through `floors(eval, set)`, so a failure names the eval, the set, the measured
value and the floor or ceiling (and the Wilson 95% interval when the score is a count), and every check, passing or
not, goes into the report. The thresholds stay in each test file; nothing here repeats them.

Report. With EVAL_REPORT=<path>, the session writes every accuracy test's outcome and checks there as JSON (the
`coffeedata.triage evals` input). A floor test that doesn't use `floors` (tests/test_bag_grams_accuracy.py, say) is
reported from its assertion message. On GitHub Actions each failed floor is also an `::error` annotation.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

# The checks the running test has stated so far (pytest_runtest_call hands them to the test's report).
_CHECKS: list[dict[str, Any]] = []
_PROPERTY = "eval_floor_checks"
_ACCURACY = re.compile(r"(?:^|/)tests/test_(?P<eval>[a-z0-9_]+)_accuracy\.py::")
# "<set>: <metric> <value> < floor <bound>", the shape the floor tests' own messages take
_MESSAGE = re.compile(
    r"^(?:(?P<set>[\w.-]+): )?(?:(?P<metric>[^<>]*?) )?(?P<value>-?[\d.,]+%?)(?: of [\d,]+)?"
    r" (?P<op>[<>]=?) (?:(?P<kind>floor|ceiling) )?(?P<bound>-?[\d.,]+%?)"
)


def eval_dir(var: str) -> Path:
    path = os.environ.get(var)
    if path:
        return Path(path)
    why = f"{var} not set: the floor's inputs are private lake data (build them with eval/floor_inputs.py)"
    if os.environ.get("REQUIRE_EVALS") == "1":
        pytest.fail(f"{why}; REQUIRE_EVALS=1 forbids skipping")
    pytest.skip(why)


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval of k successes in n."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def fmt(x: float, unit: str) -> str:
    return f"{x:,.0f}" if unit == "count" else f"{x:.2%}"


class Floors:
    """One eval set's checks. Each is recorded; the block fails once, at its end, listing every check that missed."""

    def __init__(self, eval_name: str, set_name: str):
        self.eval, self.set = eval_name, set_name
        self.missed: list[str] = []
        self.detail = ""

    def _check(self, metric: str, value: float, bound: float, kind: str, ok: bool, unit: str, n, k) -> None:
        interval = None
        if n:  # a count out of n: the interval of the share it measures
            lo, hi = wilson(value if k is None else k, n)
            interval = [round(lo, 4), round(hi, 4)]
        op = ">=" if kind == "floor" else "<="
        _CHECKS.append({
            "eval": self.eval, "set": self.set, "metric": metric, "value": value, "bound": bound, "kind": kind,
            "op": op, "ok": ok, "unit": unit, "n": n, "interval": interval,
        })  # fmt: skip
        if not ok:
            shown = f"{fmt(value, unit)}" + (f" of {n:,}" if unit == "count" and n else "")
            ci = f" (Wilson 95% {interval[0]:.1%}-{interval[1]:.1%})" if interval else ""
            bad = "<" if kind == "floor" else ">"
            self.missed.append(
                f"eval {self.eval}, set {self.set}: {metric} {shown} {bad} {kind} {fmt(bound, unit)}{ci}"
            )

    def at_least(self, metric: str, value: float, floor: float, *, unit="share", n=None, k=None) -> None:
        """value must be at least the floor. With n, value (or k) is a count of n, for the Wilson interval."""
        self._check(metric, value, floor, "floor", value >= floor, unit, n, k)

    def at_most(self, metric: str, value: float, ceiling: float, *, unit="share", n=None, k=None) -> None:
        """value must be at most the ceiling (a rate or count of confidently wrong answers)."""
        self._check(metric, value, ceiling, "ceiling", value <= ceiling, unit, n, k)


@contextmanager
def floors(eval_name: str, set_name: str) -> Iterator[Floors]:
    f = Floors(eval_name, set_name)
    yield f
    if f.missed:
        raise AssertionError("\n".join(f.missed) + (f"\n{f.detail}" if f.detail else ""))


# --- the report (hooks imported by tests/conftest.py) ----------------------------------------------------------

_RESULTS: dict[str, dict[str, Any]] = {}
_PRIVATE = "eval_floor_private"


def pytest_itemcollected(item):
    """Which workflow owns a floor: a private_eval floor is eval-floors', every other one ci's. Only the floor
    files (tests/test_<eval>_accuracy.py) are reported: a unit test that calls `floors` (tests/test_triage.py) is
    never taken for an eval, so ci's report can't say a private floor passed."""
    if _ACCURACY.search(item.nodeid):
        item.user_properties.append((_PRIVATE, item.get_closest_marker("private_eval") is not None))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _CHECKS.clear()
    yield
    item.user_properties.append((_PROPERTY, list(_CHECKS)))
    _CHECKS.clear()


def _from_message(text: str) -> dict[str, Any] | None:
    """The check a plain floor assertion states ("train: weighted accuracy 0.9412 < floor 0.95")."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    m = _MESSAGE.match(first.removeprefix("AssertionError: "))
    if not m:
        return None
    kind = m["kind"] or ("floor" if m["op"].startswith("<") else "ceiling")

    def num(s: str) -> float:
        return float(s.replace(",", "").rstrip("%")) / (100 if s.endswith("%") else 1)

    return {"set": m["set"], "metric": (m["metric"] or "").strip(), "value": num(m["value"]), "bound": num(m["bound"]),
            "kind": kind, "op": ">=" if kind == "floor" else "<=", "ok": False, "unit": "share", "n": None,
            "interval": None}  # fmt: skip


def _set_of(nodeid: str) -> str:
    """The parametrized set ("...[train]"), else the test's own name (a test of one set, or a gate)."""
    m = re.search(r"\[(.+)\]$", nodeid)
    return m[1] if m else nodeid.rsplit("::", 1)[-1].removeprefix("test_")


def pytest_runtest_logreport(report):
    m = _ACCURACY.search(report.nodeid)
    if not m:
        return
    props = dict(report.user_properties)
    checks = props.get(_PROPERTY)
    r = _RESULTS.setdefault(report.nodeid, {
        "nodeid": report.nodeid, "eval": m["eval"], "set": _set_of(report.nodeid),
        "private": bool(props.get(_PRIVATE)), "outcome": "passed", "checks": [], "message": "",
    })  # fmt: skip
    if report.when == "call" and checks:
        r["checks"] = checks
        r["set"] = checks[0]["set"]
    if report.skipped:
        r["outcome"] = "skipped"
    elif report.failed:
        r["outcome"] = "failed" if report.when == "call" else "error"
        crash = getattr(getattr(report.longrepr, "reprcrash", None), "message", None) or str(report.longrepr)
        r["message"] = crash.removeprefix("AssertionError: ")[:2000]
        if not any(not c["ok"] for c in r["checks"]) and (parsed := _from_message(crash)):
            r["checks"].append(parsed | {"eval": r["eval"], "set": parsed["set"] or r["set"]})


def _is_worker(config) -> bool:
    return hasattr(config, "workerinput")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _is_worker(config):
        return
    failed = [r for r in _RESULTS.values() if r["outcome"] in ("failed", "error")]
    if not failed:
        return
    terminalreporter.section("accuracy floors that failed")
    for r in failed:
        lines = r["message"].splitlines()[:3] or ["(no message)"]
        terminalreporter.write_line(f"{r['nodeid']}: {lines[0]}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            title = (
                f"Eval floor {r['eval']} / {r['set']}".replace("%", "%25")
                .replace(",", "%2C")
                .replace(":", "%3A")
            )
            text = "%0A".join(x.replace("%", "%25") for x in lines)
            terminalreporter.write_line(f"::error title={title}::{text}")


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("EVAL_REPORT")
    if not path or _is_worker(session.config):
        return
    doc = {
        "version": 2,
        "commit": os.environ.get("GITHUB_SHA"),
        "tests": sorted(_RESULTS.values(), key=lambda r: r["nodeid"]),
    }
    Path(path).write_text(json.dumps(doc, indent=1) + "\n")
