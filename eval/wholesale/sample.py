"""Draw one set's labeling sample: up to K listings from each stratum (F, B, N; see rules.stratum), seeded by the
set's name, shuffled together and printed with no stratum or prediction, for labelers to label blind.

Usage: python sample.py <EVAL_DIR> <train|validation|test> <K>. Writes <EVAL_DIR>/work/<set>_sample.json.
The sets in this eval were drawn with K=120 (train, F and B only, plus 40 from N), 50 (validation) and 50 (test).
"""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rules import stratum  # noqa: E402


def main(eval_dir: str, name: str, k: str) -> None:
    work = Path(eval_dir) / "work"
    pop = [r for r in json.loads(Path(work / "population.json").read_text()) if r["set"] == name]
    out = []
    for s in "FBN":
        group = [r for r in pop if stratum(r) == s]
        random.Random(f"{name}-{s}").shuffle(group)
        out += group[: int(k)]
    random.Random(name).shuffle(out)
    Path(work / f"{name}_sample.json").write_text(json.dumps([r["coffee_id"] for r in out]))
    for i, r in enumerate(out):
        grams = ",".join(map(str, sorted(set(r["grams"]))[:6]))
        print(
            f"{i}\t{(r['title'] or '')[:90]}\t{r['product_type'][:30]}\t{'; '.join(r['tags'][:8])}\t{grams}\t{r['roaster_id']}"
        )


if __name__ == "__main__":
    main(*sys.argv[1:4])
