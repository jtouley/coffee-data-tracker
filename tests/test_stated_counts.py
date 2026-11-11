"""Catalog counts are computed from each build's data, never written into the copy by hand.

Everything a visitor reads (the site's templates and script, the app's pages and scripts, the README, the dataset
cards, the listing copy) is scanned for a fixed catalog count such as "about 10,000 single-origin coffees", "~10k listings" or
"3,205 roasters". The site states its counts from the release (`site.LISTED`, `site.SINGLE_ORIGIN`). The one
exception is the marketplace listing copy, which is typed by hand into Snowflake: each of its claimed numbers, at the
line coverage_claims names, is checked by every build against its own data (marketplace/README.md, "Coverage
claims")."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from coffeedata import quality

ROOT = Path(__file__).resolve().parents[1]

# What a visitor reads. The site and app templates render counts from data; everything else states none.
PUBLIC_COPY = sorted(
    {
        *ROOT.glob("src/coffeedata/templates/**/*.html"),
        *ROOT.glob("src/coffeedata/templates/**/*.js"),
        *ROOT.glob("app/templates/**/*.html"),
        *ROOT.glob("app/static/**/*.js"),
        *ROOT.glob("app/*.js"),
        *ROOT.glob("marketplace/**/*.md"),
        ROOT / "README.md",
        ROOT / "docs" / "DATASET_CARD.md",
        ROOT / "docs" / "PUBLIC_DATASET_CARD.md",
    }
)

# A number of a thousand or more ("10,000", "3205", "10k", "~10K", "20,000+", "1.2 million", "20 thousand"), then
# within three words a thing the catalog counts ("3,200-roaster" too). A bare year (1900-2099, no comma) isn't one.
_COUNT = re.compile(
    r"(?<![\w.$/-])(?:~\s*)?"
    r"(?:\d+(?:\.\d+)?\s+(?:million|thousand)|\d{1,3}(?:,\d{3})+|(?!(?:19|20)\d\d\b)\d{4,}|\d+(?:\.\d+)?\s?[kK])\+?"
    r"(?:\s+[\w-]+){0,3}?[\s-]+"
    r"(?:single[- ]origins?|coffees?|listings?|roasters?|origins?|products?|bags?|countries)\b",
    re.I,
)


def stated_counts(text: str) -> list[str]:
    return [m.group(0) for m in _COUNT.finditer(text)]


def stated_counts_by_line(text: str) -> list[tuple[int, str]]:
    """Each count with the line it starts on; a count may run across a line break."""
    return [
        (text.count("\n", 0, m.start()) + 1, re.sub(r"\s+", " ", m.group(0))) for m in _COUNT.finditer(text)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "about 10,000 single-origin coffees",
        "~10k single-origin listings",
        "Over 10K coffees",
        "3,205 roasters from Overture Maps",
        "a seed list of about 3200 US roasters",
        "20,000+ coffees from 1,800 US specialty roasters",
        "a 3,200-roaster seed list",
        "1.2 million coffees",
        "20 thousand coffees",
        "about 38,000 coffees listed for sale, from\n1,800 US specialty roasters",
    ],
)
def test_the_scan_catches_a_fixed_count(text):
    assert stated_counts(text)


@pytest.mark.parametrize(
    "text",
    [
        '{{ "{:,}".format(totals.coffees) }} coffees listed',
        "make weekly SAMPLE=50  # 50 roasters into ./lake",
        "every 200 roasters during ingest",
        "price per 100 g",
        "size_categories: [10K<n<100K]",
        "Since 2024 the roasters",
        "the 2026 roasters list",
    ],
)
def test_the_scan_leaves_template_fields_and_small_numbers_alone(text):
    assert not stated_counts(text)


@pytest.mark.parametrize("path", PUBLIC_COPY, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_public_copy_states_a_fixed_catalog_count(path):
    text = path.read_text()
    allowed: dict[int, list[str]] = {}
    if path == quality.LISTING:
        # Typed by hand into Snowflake, so fixed by necessity. Allowed only where coverage_claims checks it: the line
        # it names (the first in the copy with the claim's text), so a second copy of the number elsewhere fails.
        for c in quality.coverage_claims():
            allowed.setdefault(int(c["source"].rsplit(":", 1)[1]), []).append(c["text"])
        # The claims comment itself, blanked line for line so the copy keeps its line numbers.
        text = re.sub(
            r"<!-- coverage-claims:.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S
        )
    found = [
        (line, c)
        for line, c in stated_counts_by_line(text)
        if not any(c in a or a in c for a in allowed.get(line, []))
    ]
    assert not found, (
        f"{path.relative_to(ROOT)} states a fixed catalog count: {found}. Compute it from the build instead "
        "(src/coffeedata/site.py), or drop it."
    )


def test_a_claimed_number_is_allowed_only_on_its_claims_line(tmp_path, monkeypatch):
    listing = tmp_path / "LISTING.md"
    listing.write_text(
        "<!-- coverage-claims:\nlisted_coffees: about 38,000 coffees\n-->\nShort: about 38,000 coffees.\n"
        "Long: about 38,000 coffees, again.\n"
    )
    monkeypatch.setattr(quality, "LISTING", listing)
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError, match=r"\[\(5, '38,000 coffees'\)\]"):
        test_no_public_copy_states_a_fixed_catalog_count(listing)


@pytest.mark.parametrize(
    ("n", "text"),
    [
        (19964, "20,000"),
        (40800, "41,000"),
        (1845, "1,800"),
        (1850, "1,900"),
        (1250, "1,300"),
        (125, "130"),
        (15759, "16,000"),
        (3205, "3,200"),
        (9950, "10,000"),
        (52, "52"),
    ],
)
def test_about_rounds_a_count_for_copy(n, text):
    assert quality.about(n) == text
