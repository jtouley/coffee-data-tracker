"""PR-review model evaluation: which free OpenCode model catches planted bugs in real PRs. See README.md.

    python pr_review_eval.py draw    <data-dir> --repo OWNER/NAME=PATH ...   once: manifest.json, the split, seals test
    python pr_review_eval.py items   <data-dir> --repo OWNER/NAME=PATH ...   rebuild items from manifest.json, check hashes
    python pr_review_eval.py predict <data-dir> <set> <model> [--limit N]   append to <data-dir>/pred/<set>/<model>.jsonl
    python pr_review_eval.py score   <data-dir> <set> <pred.jsonl>... [--examples N]
                                                                            Wilson 95% intervals; --examples: train only

Items are built from git, so only manifest.json (PR list, commits, split, planted bugs) and the test items' hashes
are in git. The data dir (items, train/validation predictions) stays out of git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tools" / "pr_review"))
import pr_review as pr  # noqa: E402

SALT = "coffeedata-prreview-eval-2026-11-04"
MANIFEST = HERE / "manifest.json"
TEST_HASHES = HERE / "test_items.sha256"
SETS = ("train", "validation", "test")
NEAR = 3  # a finding within this many lines of the planted bug, in the same file, catches it
MUTANTS_PER_PR = 2
STOP_AFTER = 5  # a model that leaves this many items in a row unanswered is down or rate-limited: stop
CODE_EXT = {".py": "py", ".sql": "sql", ".js": "js", ".ts": "js", ".mjs": "js"}


def h(*parts: object) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def split_of(repo: str, number: int) -> str:
    n = h(SALT, f"{repo}#{number}") % 100
    return "train" if n < 60 else "validation" if n < 80 else "test"


# ---- pull requests from git history ----


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout


def find_prs(path: Path, ref: str) -> list[dict]:
    """Merged PRs on the first-parent history of `ref`: merge commits and squash merges ("... (#N)")."""
    prs = {}
    for line in git(path, "log", ref, "--first-parent", "--format=%H %P\t%s").splitlines():
        shas, subject = line.split("\t", 1)
        commit, *parents = shas.split()
        m = re.match(r"Merge pull request #(\d+) ", subject)
        if m and len(parents) == 2:
            prs.setdefault(
                int(m.group(1)), {"commit": commit, "base": parents[0], "head": parents[1], "kind": "merge"}
            )
            continue
        m = re.search(r"\(#(\d+)\)$", subject)
        if m and len(parents) == 1:
            prs.setdefault(
                int(m.group(1)), {"commit": commit, "base": parents[0], "head": commit, "kind": "squash"}
            )
    return [{"number": n, **v} for n, v in sorted(prs.items())]


def raw_diff(path: Path, p: dict) -> str:
    rng = f"{p['base']}...{p['head']}" if p["kind"] == "merge" else f"{p['base']}..{p['head']}"
    return git(path, "-c", "core.quotepath=off", "diff", "--no-color", "--no-ext-diff", "-M", "-U3",
               "--src-prefix=a/", "--dst-prefix=b/", rng)  # fmt: skip


# ---- planted bugs ----

_SQL_FLAGS = re.I
OPERATORS: dict[str, list[tuple[str, str, str]]] = {
    # (name, pattern, replacement); patterns are tried at every match position outside strings and comments
    "py": [
        ("lte->lt", r"(?<![<>=!])<=(?!=)", "<"),
        ("gte->gt", r"(?<![<>=!-])>=(?!=)", ">"),
        ("lt->lte", r"(?<![<>=!-])<(?![<=])", "<="),
        ("gt->gte", r"(?<![<>=!-])>(?![>=])", ">="),
        ("eq->ne", r"(?<![<>=!])==(?!=)", "!="),
        ("ne->eq", r"!=", "=="),
        ("and->or", r"(?<=\s)and(?=\s)", "or"),
        ("or->and", r"(?<=\s)or(?=\s)", "and"),
        ("drop-not", r"(?<![\w.])(?<!is )not\s+(?!in\b)", ""),
        ("not-in->in", r"(?<=\s)not in(?=\s)", "in"),
        ("is-none->is-not", r"\bis None\b", "is not None"),
        ("is-not-none->is", r"\bis not None\b", "is None"),
        ("true->false", r"(?<=[=(,\s])True\b", "False"),
        ("false->true", r"(?<=[=(,\s])False\b", "True"),
        ("min->max", r"(?<![\w.])min\(", "max("),
        ("max->min", r"(?<![\w.])max\(", "min("),
        ("drop-plus-1", r"\s\+\s1\b(?!\.)", ""),
        ("minus-1->plus-1", r"(?<=\s)-\s1\b(?!\.)", "+ 1"),
    ],
    "sql": [
        ("lte->lt", r"(?<![<>=!])<=", "<"),
        ("gte->gt", r"(?<![<>=!-])>=", ">"),
        ("lt->lte", r"(?<![<>=!-])<(?![<=>])", "<="),
        ("gt->gte", r"(?<![<>=!-])>(?![>=])", ">="),
        ("ne->eq", r"<>|!=", "="),
        ("and->or", r"(?<=\s)and(?=\s)", "or"),
        ("or->and", r"(?<=\s)or(?=\s)", "and"),
        ("left->inner", r"\bleft\s+(?:outer\s+)?join\b", "join"),
        ("is-null->is-not", r"\bis null\b", "is not null"),
        ("is-not-null->is", r"\bis not null\b", "is null"),
        ("count-distinct", r"\bcount\(\s*distinct\s+", "count("),
        ("min->max", r"(?<![\w.])min\(", "max("),
        ("max->min", r"(?<![\w.])max\(", "min("),
        ("asc->desc", r"\basc\b", "desc"),
        ("desc->asc", r"\bdesc\b", "asc"),
    ],
    "js": [
        ("seq->sne", r"===", "!=="),
        ("sne->seq", r"!==", "==="),
        ("and->or", r"&&", "||"),
        ("or->and", r"\|\|", "&&"),
        ("lte->lt", r"(?<![<>=!])<=", "<"),
        ("gte->gt", r"(?<![<>=!])>=(?!>)", ">"),
        ("true->false", r"(?<=[=(,:\s])true\b", "false"),
        ("false->true", r"(?<=[=(,:\s])false\b", "true"),
        ("drop-plus-1", r"\s\+\s1\b(?!\.)", ""),
    ],
}


def code_lang(path: str) -> str | None:
    if re.search(r"(^|/)(tests?|eval|docs|fixtures|migrations)/|(^|/)test_[^/]*$|\.test\.|_test\.", path):
        return None
    return CODE_EXT.get(Path(path).suffix)


def live_mask(text: str, lang: str) -> list[bool]:
    """True for characters outside string literals and comments (one line, so a rough scan)."""
    comment = {"py": "#", "sql": "--", "js": "//"}[lang]
    mask, quote, i = [True] * len(text), None, 0
    while i < len(text):
        c = text[i]
        if quote:
            mask[i] = False
            if c == "\\":
                if i + 1 < len(text):
                    mask[i + 1] = False
                i += 2
                continue
            if c == quote:
                quote = None
        elif text.startswith(comment, i):
            for j in range(i, len(text)):
                mask[j] = False
            break
        elif c in "'\"`":
            quote = c
            mask[i] = False
        i += 1
    return mask


def candidates(files: list[pr.FileDiff]) -> list[dict]:
    """Every single-operator change that could be made to an added line of non-test code the reviewer sees."""
    out = []
    _, meta = pr.prepare_files(files)
    for f in files:
        lang = code_lang(f.path)
        if lang is None or f.path not in meta["files"]:
            continue
        in_doc = False
        for ln in pr.shown_lines(f):
            triple = ln.text.count('"""') + ln.text.count("'''") if lang == "py" else 0
            was_doc = in_doc
            if triple % 2:
                in_doc = not in_doc
            if ln.kind != "+" or was_doc or triple or "{{" in ln.text and lang == "py":
                continue
            stripped = ln.text.strip()
            if not stripped or stripped.startswith(("#", "--", "//", "*", "@")):
                continue
            mask = live_mask(ln.text, lang)
            for name, pat, rep in OPERATORS[lang]:
                flags = _SQL_FLAGS if lang == "sql" else 0
                for k, m in enumerate(re.finditer(pat, ln.text, flags)):
                    if not all(mask[m.start() : max(m.end(), m.start() + 1)]):
                        continue
                    new = ln.text[: m.start()] + rep + ln.text[m.end() :]
                    if new != ln.text:
                        out.append({"file": f.path, "line": ln.new_no, "op": name, "k": k,
                                    "before": ln.text, "after": new})  # fmt: skip
    return out


def pick_mutants(key: str, cands: list[dict]) -> list[dict]:
    ranked = sorted(cands, key=lambda c: h(SALT, key, c["file"], c["line"], c["op"], c["k"]))
    chosen: list[dict] = []
    for c in ranked:
        if all(c["file"] != d["file"] or abs(c["line"] - d["line"]) > 20 for d in chosen):
            chosen.append(c)
        if len(chosen) == MUTANTS_PER_PR:
            break
    return chosen


def apply_mutant(files: list[pr.FileDiff], m: dict) -> None:
    for f in files:
        if f.path != m["file"]:
            continue
        for hunk in f.hunks:
            for ln in hunk.lines:
                if ln.kind == "+" and ln.new_no == m["line"]:
                    assert ln.text == m["before"], (m, ln.text)
                    ln.text = m["after"]
                    return
    raise ValueError(f"planted line not found: {m}")


def render(files: list[pr.FileDiff]) -> str:
    return pr.prepare_files(files)[0]


def make_items(repo: str, entry: dict, path: Path) -> list[dict]:
    files = pr.parse_diff(raw_diff(path, entry))
    key = f"{repo}#{entry['number']}"
    items = [
        {"id": f"{key}:clean", "pr": key, "split": entry["split"], "mutant": None, "diff": render(files)}
    ]
    for i, m in enumerate(entry["mutants"], 1):
        mfiles = pr.parse_diff(raw_diff(path, entry))
        apply_mutant(mfiles, m)
        items.append(
            {"id": f"{key}:m{i}", "pr": key, "split": entry["split"], "mutant": m, "diff": render(mfiles)}
        )
    return items


def item_hash(item: dict) -> str:
    return hashlib.sha256(item["diff"].encode()).hexdigest()


def write_items(data: Path, items: list[dict]) -> None:
    for s in SETS:
        with open(data / f"items_{s}.jsonl", "w") as fh:
            for it in items:
                if it["split"] == s:
                    fh.write(json.dumps(it) + "\n")


def repos_arg(values: list[str]) -> dict[str, Path]:
    return {k: Path(v) for k, v in (x.split("=", 1) for x in values)}


def cmd_draw(data: Path, repos: dict[str, Path], ref: str) -> None:
    if MANIFEST.exists():
        sys.exit(f"{MANIFEST} exists: the draw is made once. Use `items` to rebuild.")
    manifest: dict = {"salt": SALT, "max_chars": pr.MAX_CHARS, "repos": {}, "excluded": {}}
    items: list[dict] = []
    for repo, path in repos.items():
        head = git(path, "rev-parse", ref).strip()
        manifest["repos"][repo] = {"ref": head, "prs": []}
        for p in find_prs(path, head):
            files = pr.parse_diff(raw_diff(path, p))
            key = f"{repo}#{p['number']}"
            muts = pick_mutants(key, candidates(files))
            if not muts:
                manifest["excluded"][key] = "no code line to plant a bug in"
                continue
            entry = {**p, "split": split_of(repo, p["number"]), "mutants": muts}
            manifest["repos"][repo]["prs"].append(entry)
            items += make_items(repo, entry, path)
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n")
    TEST_HASHES.write_text("".join(f"{item_hash(i)}  {i['id']}\n" for i in items if i["split"] == "test"))
    data.mkdir(parents=True, exist_ok=True)
    write_items(data, items)
    summarize(items, manifest)


def cmd_items(data: Path, repos: dict[str, Path]) -> None:
    manifest = json.loads(MANIFEST.read_text())
    items = []
    for repo, info in manifest["repos"].items():
        if repo not in repos:
            sys.exit(f"--repo {repo}=PATH is needed")
        for entry in info["prs"]:
            items += make_items(repo, entry, repos[repo])
    sealed = dict(line.split("  ")[::-1] for line in TEST_HASHES.read_text().splitlines())
    built = {i["id"]: item_hash(i) for i in items if i["split"] == "test"}
    if {k: v.strip() for k, v in sealed.items()} != built:
        sys.exit("test items don't match test_items.sha256: the history or the item builder changed")
    data.mkdir(parents=True, exist_ok=True)
    write_items(data, items)
    summarize(items, manifest)


def summarize(items: list[dict], manifest: dict) -> None:
    for s in SETS:
        sub = [i for i in items if i["split"] == s]
        print(f"{s:<10} {len({i['pr'] for i in sub}):>4} PRs  {len(sub):>4} items "
              f"({sum(i['mutant'] is not None for i in sub)} with a planted bug)")  # fmt: skip
    print(f"excluded   {len(manifest['excluded'])} PRs")


# ---- predictions ----


def load_items(data: Path, s: str) -> list[dict]:
    return [json.loads(line) for line in (data / f"items_{s}.jsonl").read_text().splitlines()]


def cmd_predict(
    data: Path, s: str, model: str, limit: int | None, workdir: Path, tries: int, timeout: int
) -> None:
    out = data / "pred" / s / (model.replace("/", "__") + ".jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        done = {json.loads(line)["id"] for line in out.read_text().splitlines() if json.loads(line).get("ok")}
    todo = [i for i in load_items(data, s) if i["id"] not in done][:limit]
    print(f"{model}: {len(todo)} items to run ({len(done)} already answered)", flush=True)
    unanswered_run = 0
    for n, it in enumerate(todo, 1):
        message = f"Pull request {it['pr'].split('#')[1]}.\n\n{it['diff']}"
        rec: dict = {"id": it["id"], "model": model}
        for attempt in range(tries):
            text, info = pr.run_model(model, message, workdir, timeout)
            rec.update(seconds=info.get("seconds"), tokens=info.get("tokens"), attempts=attempt + 1)
            if text is not None:
                parsed = pr.parse_answer(text)
                rec.update(ok=parsed["ok"], decision=parsed["decision"], findings=parsed["findings"],
                           error=parsed["error"], raw=text)  # fmt: skip
                break
            rec.update(ok=False, error=info.get("error"), unanswered=True)
            if attempt + 1 < tries:
                time.sleep(30 * (attempt + 1))
        with open(out, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"[{n}/{len(todo)}] {it['id']} ok={rec.get('ok')} {rec.get('decision')} "
              f"{len(rec.get('findings') or [])} findings {rec.get('seconds')}s", flush=True)  # fmt: skip
        unanswered_run = unanswered_run + 1 if rec.get("unanswered") else 0
        if unanswered_run >= STOP_AFTER:
            sys.exit(
                f"{model}: {STOP_AFTER} items in a row unanswered, stopping. Last error: {rec.get('error')}"
            )


# ---- scoring ----


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (math.nan, math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, c - w), min(1.0, c + w))


def same_file(a: str, b: str) -> bool:
    a, b = a.strip().lstrip("./"), b.strip().lstrip("./")
    return bool(a) and (a == b or a.endswith("/" + b) or b.endswith("/" + a))


def hits(findings: list[dict], m: dict, severities: tuple[str, ...] = pr.SEVERITIES) -> bool:
    return any(
        f["severity"] in severities and same_file(f["file"], m["file"]) and f["line"] is not None
        and abs(f["line"] - m["line"]) <= NEAR
        for f in findings
    )  # fmt: skip


def load_preds(path: Path) -> dict[str, dict]:
    recs: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if r["id"] not in recs or r.get("ok"):  # a later answer replaces an earlier failure
            recs[r["id"]] = r
    return recs


def score_one(items: list[dict], preds: dict[str, dict]) -> dict:
    clean = {i["pr"]: i for i in items if i["mutant"] is None}
    rows: dict[str, list[int]] = {k: [0, 0] for k in
            ("answered", "catch", "catch_serious", "background", "mutant_blocked", "clean_blocked", "catch_all")}  # fmt: skip
    seconds = []
    for it in items:
        p = preds.get(it["id"], {})
        ok = bool(p.get("ok"))
        rows["answered"][0] += ok
        rows["answered"][1] += 1
        if p.get("seconds") is not None and ok:
            seconds.append(p["seconds"])
        if it["mutant"] is not None:
            caught = ok and hits(p["findings"], it["mutant"])
            rows["catch_all"][0] += caught
            rows["catch_all"][1] += 1
        if not ok:
            continue
        if it["mutant"] is None:
            rows["clean_blocked"][0] += p["decision"] == "NO MERGE"
            rows["clean_blocked"][1] += 1
            continue
        m = it["mutant"]
        rows["catch"][0] += hits(p["findings"], m)
        rows["catch"][1] += 1
        rows["catch_serious"][0] += hits(p["findings"], m, ("blocker", "major"))
        rows["catch_serious"][1] += 1
        rows["mutant_blocked"][0] += p["decision"] == "NO MERGE"
        rows["mutant_blocked"][1] += 1
        c = preds.get(clean[it["pr"]]["id"], {})
        if c.get("ok"):
            rows["background"][0] += hits(c["findings"], m)
            rows["background"][1] += 1
    out: dict = {k: (v[0], v[1], *wilson(*v)) for k, v in rows.items()}
    out["median_seconds"] = statistics.median(seconds) if seconds else math.nan
    out["net_catch"] = out["catch"][2] - out["background"][2]
    return out


LABELS = {
    "answered": "answered (parseable)",
    "catch_all": "caught, unanswered = missed",
    "catch": "caught (answered)",
    "catch_serious": "caught as blocker/major",
    "background": "same spot flagged on clean",
    "mutant_blocked": "NO MERGE, planted bug",
    "clean_blocked": "NO MERGE, clean PR",
}


def cmd_score(data: Path, s: str, paths: list[Path], examples: int) -> None:
    if examples and s != "train":
        sys.exit("--examples is for train only")
    items = load_items(data, s)
    results = {}
    for path in paths:
        preds = load_preds(path)
        name = next(iter(preds.values()))["model"] if preds else path.stem
        results[name] = score_one(items, preds)
    print(f"{s}: {len({i['pr'] for i in items})} PRs, {len(items)} items. Wilson 95% intervals.\n")
    for name, r in sorted(results.items(), key=lambda kv: -kv[1]["net_catch"]):
        print(f"{name}  (net catch {r['net_catch']:.1%}, median {r['median_seconds']:.0f}s)")
        for k, label in LABELS.items():
            k_, n, p, lo, hi = r[k]
            print(f"  {label:<30} {k_:>4}/{n:<4} {p:6.1%}  [{lo:.1%}, {hi:.1%}]")
        print()
    if examples:
        preds = load_preds(paths[0])
        missed = [i for i in items if i["mutant"] and preds.get(i["id"], {}).get("ok")
                  and not hits(preds[i["id"]]["findings"], i["mutant"])]  # fmt: skip
        for it in missed[:examples]:
            m, p = it["mutant"], preds[it["id"]]
            print(f"--- missed {it['id']} {m['file']}:{m['line']} {m['op']}\n  - {m['before'].strip()}\n"
                  f"  + {m['after'].strip()}\n  findings: " + json.dumps(
                      [(f["file"], f["line"], f["severity"], f["title"]) for f in p["findings"]]))  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("draw", "items"):
        p = sub.add_parser(name)
        p.add_argument("data", type=Path)
        p.add_argument(
            "--repo", action="append", required=True, help="OWNER/NAME=PATH to a clone with full history"
        )
        p.add_argument("--ref", default="origin/main")
    p = sub.add_parser("predict")
    p.add_argument("data", type=Path)
    p.add_argument("set", choices=SETS)
    p.add_argument("model", help="provider/model, e.g. opencode/big-pickle")
    p.add_argument("--limit", type=int)
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--timeout", type=int, default=600, help="seconds per run")
    p.add_argument("--workdir", type=Path, default=HERE.parents[1], help="where .opencode/agents/ is")
    p = sub.add_parser("score")
    p.add_argument("data", type=Path)
    p.add_argument("set", choices=SETS)
    p.add_argument("preds", type=Path, nargs="+")
    p.add_argument("--examples", type=int, default=0)
    a = ap.parse_args()
    if a.cmd == "draw":
        cmd_draw(a.data, repos_arg(a.repo), a.ref)
    elif a.cmd == "items":
        cmd_items(a.data, repos_arg(a.repo))
    elif a.cmd == "predict":
        cmd_predict(a.data, a.set, a.model, a.limit, a.workdir, a.tries, a.timeout)
    else:
        cmd_score(a.data, a.set, a.preds, a.examples)


if __name__ == "__main__":
    main()
