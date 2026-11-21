"""Rebuild the labelers' files from the lake and manifest/<set>.tsv: no predictions, verdicts or strata involved.

  python make_blind.py <lake copy> <data dir> <set> [--labeler A|B] [--only id,id,...] [--download <hf dataset>]

Writes <data dir>/<set>/blind.json (checked against SHA256SUMS when it lists the file) and, with --labeler, the
same products as text files of 40, <data dir>/<set>/<labeler>/part_NN.txt, in that labeler's own order. --only
keeps the listed ids (the third labeler's disagreements). It prints counts and paths only.
"""

import hashlib
import sys
from pathlib import Path

from common import LABELER_FILES, blind_text, copy_lake, h, products, write_blind

HERE = Path(__file__).parent


def arg(flag: str) -> str | None:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def main() -> None:
    lake, data, name = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    if repo := arg("--download"):
        copy_lake(repo, lake, LABELER_FILES)
    lines = (HERE / "manifest" / f"{name}.tsv").read_text().splitlines()
    ids = {k: int(i) for i, k in (line.split("\t") for line in lines)}
    items = []
    for m in products(lake):
        if (i := ids.get(h(m["item_key"])[:16])) is not None:
            items.append(m | {"id": i})
    items.sort(key=lambda m: m["id"])
    if len(items) != len(ids):
        sys.exit(
            f"{name}: {len(items)} of {len(ids)} products found; the lake differs from the copy the sample used"
        )
    d = data / name
    d.mkdir(parents=True, exist_ok=True)
    write_blind(items, d / "blind.json")
    digest = hashlib.sha256((d / "blind.json").read_bytes()).hexdigest()
    sums = HERE / "SHA256SUMS"
    listed = (
        {p: s for s, p in (line.split() for line in sums.read_text().splitlines())} if sums.exists() else {}
    )
    if listed.get(f"{name}/blind.json", digest) != digest:
        sys.exit(f"{name}/blind.json differs from SHA256SUMS; the lake differs from the copy the sample used")
    print(f"{name}: {len(items)} products -> {d / 'blind.json'}")
    if labeler := arg("--labeler"):
        only = {int(i) for i in arg("--only").split(",")} if arg("--only") else None
        mine = [m for m in items if only is None or m["id"] in only]
        if labeler != "A":
            mine.sort(key=lambda m: h(f"labeler {labeler}:{m['id']}"))
        out = d / labeler
        out.mkdir(exist_ok=True)
        for n in range(0, len(mine), 40):
            text = "\n\n".join(blind_text(m) for m in mine[n : n + 40])
            (out / f"part_{n // 40 + 1:02}.txt").write_text(text + "\n")
        print(f"labeler {labeler}: {len(mine)} products in {(len(mine) + 39) // 40} files under {out}")


if __name__ == "__main__":
    main()
