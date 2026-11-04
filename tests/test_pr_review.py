"""The OpenCode PR review's diff preparation and answer parsing, and its evaluation's planted bugs and scoring."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pr_review"))
import pr_review as pr  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "pr_review_eval", ROOT / "eval" / "pr_review" / "pr_review_eval.py"
)
ev = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(ev)  # type: ignore[union-attr]

DIFF = """\
diff --git a/app/x.py b/app/x.py
index 1111111..2222222 100644
--- a/app/x.py
+++ b/app/x.py
@@ -10,4 +10,5 @@ def f(a, b):
     keep = 1
-    if a == b:
+    if a == b and b is not None:
+        return "a == b"  # same
     return False
diff --git a/uv.lock b/uv.lock
--- a/uv.lock
+++ b/uv.lock
@@ -1 +1 @@
-x
+y
diff --git a/tests/test_x.py b/tests/test_x.py
new file mode 100644
--- /dev/null
+++ b/tests/test_x.py
@@ -0,0 +1,1 @@
+assert 1 == 1
"""


def test_parse_numbers_new_lines_and_skips_removed():
    files = pr.parse_diff(DIFF)
    assert [f.path for f in files] == ["app/x.py", "uv.lock", "tests/test_x.py"]
    assert files[2].status == "added"
    lines = files[0].hunks[0].lines
    assert [(ln.kind, ln.new_no) for ln in lines] == [(" ", 10), ("-", None), ("+", 11), ("+", 12), (" ", 13)]


def test_prepare_annotates_and_leaves_out_lockfiles():
    text, meta = pr.prepare(DIFF)
    assert "### app/x.py (modified)" in text
    assert "  11 +     if a == b and b is not None:" in text
    assert "     -     if a == b:" in text
    assert meta["left_out"] == ["uv.lock"]
    assert "[Left out of this diff: uv.lock]" in text


def test_prepare_caps_size():
    _, meta = pr.prepare(DIFF, max_chars=10)
    assert meta["files"] == []


ANSWER = """## SUMMARY

**Status: ❌ NO MERGE**

```json
{"decision": "no_merge", "findings": [{"file": "./app/x.py", "line": "11", "severity": "BLOCKER", "title": "t"},
 {"file": "app/x.py", "line": null, "severity": "weird"}]}
```
"""


def test_parse_answer_normalizes():
    r = pr.parse_answer(ANSWER)
    assert r["ok"] and r["decision"] == "NO MERGE"
    assert r["findings"][0] == {
        "file": "app/x.py",
        "line": 11,
        "severity": "blocker",
        "title": "t",
        "detail": "",
    }
    assert r["findings"][1]["line"] is None and r["findings"][1]["severity"] == "minor"
    assert "```" not in r["markdown"] and "NO MERGE" in r["markdown"]


def test_parse_answer_rejects_missing_or_bad_json():
    assert pr.parse_answer("no block")["error"] == "no json block"
    assert not pr.parse_answer('```json\n{"decision": "maybe"}\n```')["ok"]
    assert not pr.parse_answer("```json\n{oops\n```")["ok"]


def test_text_from_events_reads_text_parts():
    out = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "step_start", "part": {"type": "step-start"}},
            {"type": "text", "part": {"type": "text", "text": "hello"}},
            {"type": "step_finish", "part": {"type": "step-finish", "tokens": {"input": 5, "output": 2}}},
        ]
    )
    assert pr.text_from_events(out) == "hello"
    assert pr.usage_from_events(out) == {"input": 5, "output": 2}


def test_comment_has_marker_and_reports_failure():
    ok = pr.comment({"ok": True, "markdown": "## SUMMARY", "model": "opencode/m"})
    assert ok.startswith(pr.COMMENT_MARKER) and "`opencode/m`" in ok
    bad = pr.comment({"ok": False, "attempts": [{"model": "opencode/m", "error": "rate limited"}]})
    assert "didn't run" in bad and "rate limited" in bad


# ---- evaluation ----


def test_candidates_skip_tests_strings_and_comments():
    cands = ev.candidates(pr.parse_diff(DIFF))
    assert {c["file"] for c in cands} == {"app/x.py"}
    assert all(c["line"] == 11 for c in cands)  # line 12's == is in a string and a comment
    ops = {c["op"] for c in cands}
    assert {"eq->ne", "and->or", "is-not-none->is"} <= ops


def test_live_mask():
    m = ev.live_mask('x = "a == b"  # c == d', "py")
    assert m[0] and not m[6] and not m[16]
    m = ev.live_mask("where a = 'x' -- b <> c", "sql")
    assert m[6] and not m[11] and not m[20]


def test_mutant_round_trip():
    files = pr.parse_diff(DIFF)
    m = next(c for c in ev.candidates(files) if c["op"] == "eq->ne")
    ev.apply_mutant(files, m)
    assert "  11 +     if a != b and b is not None:" in ev.render(files)


def test_split_is_deterministic_and_grouped_by_pr():
    assert ev.split_of("o/r", 7) == ev.split_of("o/r", 7)
    counts = {s: 0 for s in ev.SETS}
    for n in range(2000):
        counts[ev.split_of("o/r", n)] += 1
    assert 0.55 < counts["train"] / 2000 < 0.65 and 0.15 < counts["test"] / 2000 < 0.25


def test_hits_needs_same_file_and_nearby_line():
    m = {"file": "app/x.py", "line": 11}
    f = {"file": "x.py", "line": 13, "severity": "minor"}
    assert ev.hits([f], m)
    assert not ev.hits([{**f, "line": 15}], m)
    assert not ev.hits([{**f, "file": "app/y.py"}], m)
    assert not ev.hits([f], m, ("blocker", "major"))


def test_score_counts_background_and_blocks():
    m = {"file": "app/x.py", "line": 11}
    items = [
        {"id": "r#1:clean", "pr": "r#1", "mutant": None},
        {"id": "r#1:m1", "pr": "r#1", "mutant": m},
    ]
    near = [{"file": "app/x.py", "line": 11, "severity": "blocker"}]
    preds = {
        "r#1:clean": {"ok": True, "decision": "MERGE", "findings": [], "seconds": 3},
        "r#1:m1": {"ok": True, "decision": "NO MERGE", "findings": near, "seconds": 5},
    }
    r = ev.score_one(items, preds)
    assert r["catch"][:2] == (1, 1) and r["background"][:2] == (0, 1)
    assert r["clean_blocked"][:2] == (0, 1) and r["mutant_blocked"][:2] == (1, 1)
    assert r["net_catch"] == 1.0


def test_wilson():
    p, lo, hi = ev.wilson(8, 10)
    assert p == 0.8 and round(lo, 3) == 0.490 and round(hi, 3) == 0.943


def test_api_key_prefers_env_then_mints_and_refreshes(monkeypatch):
    calls = []
    clock = [1000.0]

    def fake_exchange(client_id):
        calls.append(client_id)
        return f"key{len(calls)}", clock[0] + 3600

    monkeypatch.setattr(pr, "exchange_oidc", fake_exchange)
    monkeypatch.setattr(pr.time, "time", lambda: clock[0])
    monkeypatch.setattr(pr, "_minted", {})
    monkeypatch.setenv("OPENCODE_API_KEY", "from-env")
    assert pr.api_key() == "from-env" and calls == []

    monkeypatch.delenv("OPENCODE_API_KEY")
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.setenv("OPENCODE_CLIENT_ID", "cid")
    assert pr.api_key() is None  # not in Actions

    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://example.invalid/?x=1")
    assert pr.api_key() == "key1" and pr.api_key() == "key1" and calls == ["cid"]
    clock[0] += 3600 - pr.REFRESH_EARLY + 1
    assert pr.api_key() == "key2"
