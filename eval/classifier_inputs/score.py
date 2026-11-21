"""Agreement and scoring for the three sets. Prints totals and 95% intervals only, never a product.

  python score.py agree <data dir> <set>
      agreement between labels_A.json and labels_B.json; writes to_settle.json (ids only) for the third labeler
  python score.py score <data dir> <set> <name>=<predictions.json>[@<stratum>] ...
      each classifier's errors per stratum, with Wilson 95% intervals, the confidently wrong ones, and accuracy
      weighted by stratum size. @<stratum> scores that stratum on the rules alone (predict.py's rules_only)
  python score.py final <data dir> <name>=<predictions.json>[@<stratum>] ...
      the same on the sealed test set, once per name: it refuses a second run (it leaves test/FINAL_SCORED)

<data dir> is a local folder, or the private dev dataset that keeps these files, as hf://datasets/<owner>/<name>:
its name must end in -dev (anything else is refused, so the lake or the public export can never be scored from or
written to by mistake), and its dev/ folder is copied to a local folder first. Nothing is ever uploaded from here.

The reference label of a product is the one labelers A and B agree on, else the third labeler's (labels_C.json).
Products whose reference is "unsure" are left out and counted.
Confidently wrong: a wrong answer the classifier gave as certain, which is one decided by a stored verdict or by
the rules' outright exclusions (classify.excluded). The rest of the wrong answers are the rules' fallback.
"""

import json
import math
import sys
import tempfile
from pathlib import Path

Z = 1.96


def wilson(k: int, n: int) -> tuple[float, float, float]:
    if n == 0:
        return (0.0, 0.0, 1.0)
    p = k / n
    den = 1 + Z * Z / n
    mid = (p + Z * Z / (2 * n)) / den
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / den
    return p, max(0.0, mid - half), min(1.0, mid + half)


def labels(path: Path) -> dict[int, str]:
    return {int(r["id"]): r["label"] for r in json.loads(path.read_text())} if path.exists() else {}


def reference(d: Path) -> dict[int, str]:
    a, b, c = labels(d / "labels_A.json"), labels(d / "labels_B.json"), labels(d / "labels_C.json")
    if set(a) != set(b):
        sys.exit("labels_A.json and labels_B.json cover different products")
    ref = {}
    for i in a:
        if a[i] == b[i]:
            ref[i] = a[i]
        elif i in c:
            ref[i] = c[i]
        else:
            sys.exit("a disagreement has no third label yet: run `agree`, then label to_settle.json")
    return ref


def agree(d: Path) -> None:
    a, b = labels(d / "labels_A.json"), labels(d / "labels_B.json")
    same = [i for i in a if a[i] == b.get(i)]
    kinds = sorted({*a.values(), *b.values()})
    pe = sum(
        (sum(v == k for v in a.values()) / len(a)) * (sum(v == k for v in b.values()) / len(b)) for k in kinds
    )
    po = len(same) / len(a)
    print(
        f"agreement {len(same)}/{len(a)} = {po:.1%}; Cohen's kappa {(po - pe) / (1 - pe):.3f}; to settle: {len(a) - len(same)}"
    )
    (d / "to_settle.json").write_text(json.dumps(sorted(i for i in a if i not in same)))


def score(d: Path, preds: list[str]) -> None:
    sample = json.loads((d / "sample.json").read_text())
    stratum = {m["id"]: m["stratum"] for m in sample["items"]}
    pop = {s: n for s, n in sample["population"].items() if n}
    ref = reference(d)
    unsure = sum(v == "unsure" for v in ref.values())
    print(
        f"set {sample['set']}: {len(ref)} labeled products, {unsure} unsure left out; population {sum(pop.values()):,}"
    )
    for spec in preds:
        name, path = spec.split("=", 1)
        path, _, without_verdict = path.partition("@")
        pred = {int(k): v for k, v in json.loads(Path(path).read_text()).items()}
        note = f" ({without_verdict} scored on the rules alone)" if without_verdict else ""
        print(f"\n{name}{note}")
        err = var = 0.0
        tot_fp = tot_fn = tot_conf = 0
        for s, size in pop.items():
            ids = [i for i in ref if stratum[i] == s and ref[i] != "unsure"]
            fp = fn = conf = 0
            for i in ids:
                p = pred[i]
                rules_only = s == without_verdict
                says = p["rules_only"] if rules_only else p["is_coffee"]
                if says != (ref[i] == "coffee"):
                    fp, fn = fp + says, fn + (not says)
                    conf += p["excluded"] or (p["by"] == "claude" and not rules_only)
            n = len(ids)
            e, lo, hi = wilson(fp + fn, n)
            w = size / sum(pop.values())
            err += w * e
            adj = (fp + fn + 2) / (n + 4)
            var += w * w * adj * (1 - adj) / (n + 4)
            tot_fp, tot_fn, tot_conf = tot_fp + fp, tot_fn + fn, tot_conf + conf
            print(f"  {s:21} n={n:3} of {size:6,}  kept-but-not-coffee={fp:2}  dropped-but-coffee={fn:2}  "
                  f"error {e:5.1%} (95% CI {lo:.1%}-{hi:.1%})  confidently wrong={conf}")  # fmt: skip
        half = Z * math.sqrt(var)
        print(f"  total: kept-but-not-coffee={tot_fp} dropped-but-coffee={tot_fn} confidently wrong={tot_conf}; "
              f"accuracy weighted by stratum size {1 - err:.2%} (95% CI {max(0, 1 - err - half):.2%}-{min(1, 1 - err + half):.2%})")  # fmt: skip


HF = "hf://datasets/"


def data_dir(arg: str) -> Path:
    """The folder holding the sets: a local path as it is, or a local copy of a -dev dataset's dev/ folder."""
    if "://" not in arg:
        return Path(arg)
    repo = arg.removeprefix(HF).split("@")[0].strip("/")
    if not arg.startswith(HF) or repo.count("/") != 1 or not repo.endswith("-dev"):
        sys.exit(f"refused: {arg} is not a dev dataset (hf://datasets/<owner>/<name>-dev)")
    from huggingface_hub import snapshot_download

    local = Path(tempfile.gettempdir()) / "classifier-eval" / repo.replace("/", "__")
    snapshot_download(repo, repo_type="dataset", local_dir=local, allow_patterns=["dev/*"])
    return local / "dev"


def main() -> None:
    cmd, data = sys.argv[1], data_dir(sys.argv[2])
    rest = sys.argv[3:]
    if cmd == "agree":
        agree(data / rest[0])
    elif cmd == "score":
        if rest[0] == "test":
            sys.exit("the test set is scored with `final`, once")
        score(data / rest[0], rest[1:])
    elif cmd == "final":
        marker = data / "test" / "FINAL_SCORED"
        done = marker.read_text().splitlines() if marker.exists() else []
        names = [r.split("=", 1)[0] for r in rest]
        if again := sorted(set(names) & set(done)):
            sys.exit(f"already scored once: {', '.join(again)}")
        marker.write_text("\n".join([*done, *names]) + "\n")
        score(data / "test", rest)


if __name__ == "__main__":
    main()
