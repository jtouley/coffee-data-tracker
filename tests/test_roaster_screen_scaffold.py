"""The roaster screen evaluation's scaffold (eval/roaster_screen/README.md; docs/design/roaster-screen.md, PR 1).

Nothing is scored here: there is no screen yet. These checks keep the imported labels as #130 left them and the
split as its README states it, so the evaluation that follows starts from what was reviewed. Only counts are
printed, never an item.
"""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
EVAL = ROOT / "eval" / "roaster_screen"
SPENT = ROOT / "tests" / "fixtures" / "roaster_screen_130"
SALT = "coffeedata-screen-eval-2026-11-07"
DIRS = {"train": EVAL, "validation": EVAL, "test": SPENT}
LABELS = {"in_scope", "out_of_scope", "unsure"}
KINDS = {"roaster", "cafe", "reseller", "other"}


def group(domain: str) -> str:
    """The split's group: the registrable domain, as the README defines it."""
    p = domain.split(".")
    if len(p) >= 3 and len(p[-1]) == 2 and p[-2] in ("co", "com", "org", "net"):
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def split_of(domain: str) -> str:
    n = int(hashlib.sha256(f"{SALT}:split:{group(domain)}".encode()).hexdigest()[:8], 16) % 100
    return "train" if n < 60 else "validation" if n < 80 else "test"


def load(set_name: str, stem: str):
    return json.loads((DIRS[set_name] / f"{stem}.json").read_text())


def test_the_split_rule_is_the_one_the_readme_states():
    # Worked by hand from the rule, so a change to the salt, the hash or the cut points fails here.
    assert int(hashlib.sha256(f"{SALT}:split:example.com".encode()).hexdigest()[:8], 16) % 100 == 79
    assert split_of("example.com") == "validation"
    assert group("shop.example.com") == "example.com"
    assert group("shop.example.co.uk") == "example.co.uk"
    assert group("example.com.au") == "example.com.au"
    assert group("example.coffee") == "example.coffee"


@pytest.mark.parametrize("directory", [EVAL, SPENT], ids=["eval", "spent-test"])
def test_imported_files_are_unchanged(directory):
    lines = (directory / "SHA256SUMS").read_text().splitlines()
    assert lines
    for line in lines:
        digest, name = line.split()
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest, name


def test_every_imported_json_file_is_pinned():
    pinned = {line.split()[1] for line in (EVAL / "SHA256SUMS").read_text().splitlines()}
    assert pinned == {p.name for p in EVAL.glob("*.json")}


def test_every_file_of_the_spent_test_set_is_pinned():
    # #130's spent test set is a regression fixture: every file in it but SHA256SUMS itself, labels and README too
    pinned = {line.split()[1] for line in (SPENT / "SHA256SUMS").read_text().splitlines()}
    assert pinned == {p.name for p in SPENT.iterdir() if p.name != "SHA256SUMS"}
    assert len(pinned) == 10


@pytest.mark.parametrize("set_name", ["train", "validation", "test"])
def test_each_item_is_in_the_set_its_domain_hashes_to(set_name):
    items = load(set_name, f"items_{set_name}")
    assert len(items) == len({i["id"] for i in items}) == 200
    assert sum(split_of(i["domain"]) == set_name for i in items) == 200


def test_no_business_is_on_two_sides():
    sides: dict[str, set[str]] = {}
    for set_name in DIRS:
        for i in load(set_name, f"items_{set_name}"):
            sides.setdefault(group(i["domain"]), set()).add(set_name)
    assert sum(len(s) > 1 for s in sides.values()) == 0


@pytest.mark.parametrize(
    ("set_name", "agreed", "to_settle", "third"),
    [
        ("train", 146, 54, "settled_C_train"),
        ("validation", 170, 30, "settled_C_validation"),
        ("test", 163, 37, "settled_test"),
    ],
)
def test_labels_cover_every_item_once(set_name, agreed, to_settle, third):
    ids = {i["id"] for i in load(set_name, f"items_{set_name}")}
    a = {r["id"]: r for r in load(set_name, f"labels_{set_name}_A")}
    b = {r["id"]: r for r in load(set_name, f"labels_{set_name}_B")}
    assert set(a) == set(b) == ids
    assert {r["label"] for r in (*a.values(), *b.values())} <= LABELS
    assert {r["kind"] for r in (*a.values(), *b.values())} <= KINDS

    # Agreed: the same label and kind, and not unsure. Everything else waits to be settled.
    same = {
        i
        for i in ids
        if (a[i]["label"], a[i]["kind"]) == (b[i]["label"], b[i]["kind"]) and a[i]["label"] != "unsure"
    }
    assert set(load(set_name, f"agreed_{set_name}")) == same
    waiting = {r["id"] for r in load(set_name, f"to_settle_{set_name}")}
    assert waiting == ids - same
    assert (len(same), len(waiting)) == (agreed, to_settle)
    assert {r["id"] for r in load(set_name, third)} == waiting


def test_the_new_settles_cover_exactly_what_was_waiting():
    # #130's third labeler's answers are kept under another name, for comparison only. The reference for a
    # disagreement is settled_<set>.json (the README says who settled, and from what): it must exist, be pinned, and
    # cover exactly the items waiting.
    pinned = {line.split()[1] for line in (EVAL / "SHA256SUMS").read_text().splitlines()}
    for set_name in ("train", "validation"):
        settles = EVAL / f"settled_{set_name}.json"
        waiting = {r["id"] for r in load(set_name, f"to_settle_{set_name}")}
        rows = json.loads(settles.read_text())
        assert {r["id"] for r in rows} == waiting and len(rows) == len(waiting)
        assert {r["label"] for r in rows} <= LABELS and {r["kind"] for r in rows} <= KINDS
        assert settles.name in pinned
