"""Catalog counts are computed from each build's data, never written into the copy by hand.

Everything a visitor reads (the site's templates, the app's pages and script, the README, the dataset cards, the
listing copy) is scanned for a fixed catalog count such as "about 10,000 single-origin coffees", "~10k listings" or
"3,205 roasters". The site states its counts from the release (`site.LISTED`, `site.SINGLE_ORIGIN`). The one
exception is the marketplace listing copy, which is typed by hand into Snowflake: its numbers are the
coverage-claims that every build checks against its own data (marketplace/README.md, "Coverage claims")."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coffeedata import quality, site

ROOT = Path(__file__).resolve().parents[1]

# What a visitor reads. The site and app templates render counts from data; everything else states none.
PUBLIC_COPY = sorted(
    {
        *ROOT.glob("src/coffeedata/templates/**/*.html"),
        *ROOT.glob("app/templates/**/*.html"),
        *ROOT.glob("app/static/**/*.js"),
        *ROOT.glob("marketplace/**/*.md"),
        ROOT / "README.md",
        ROOT / "docs" / "DATASET_CARD.md",
        ROOT / "docs" / "PUBLIC_DATASET_CARD.md",
    }
)

# A number of a thousand or more ("10,000", "3205", "10k", "~10K", "20,000+"), optionally hedged, then within three
# words a thing the catalog counts.
_COUNT = re.compile(
    r"(?<![\w.$/-])(?:~\s*)?(?:\d{1,3}(?:,\d{3})+|\d{4,}|\d+(?:\.\d+)?\s?[kK])\+?"
    r"(?:\s+[\w-]+){0,3}?\s+"
    r"(?:single[- ]origins?|coffees?|listings?|roasters?|origins?|products?|bags?|countries)\b",
    re.I,
)


def stated_counts(text: str) -> list[str]:
    return [m.group(0) for m in _COUNT.finditer(re.sub(r"\s+", " ", text))]


@pytest.mark.parametrize(
    "text",
    [
        "about 10,000 single-origin coffees",
        "~10k single-origin listings",
        "Over 10K coffees",
        "3,205 roasters from Overture Maps",
        "a seed list of about 3200 US roasters",
        "20,000+ coffees from 1,800 US specialty roasters",
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
    ],
)
def test_the_scan_leaves_template_fields_and_small_numbers_alone(text):
    assert not stated_counts(text)


@pytest.mark.parametrize("path", PUBLIC_COPY, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_public_copy_states_a_fixed_catalog_count(path):
    text = path.read_text()
    allowed = set()
    if path == quality.LISTING:
        # Typed by hand into Snowflake, so fixed by necessity; every build checks each one (coverage_claims_*).
        allowed = {c["text"] for c in quality.coverage_claims()}
        text = re.sub(r"<!-- coverage-claims:.*?-->", "", text, flags=re.S)
    found = [c for c in stated_counts(text) if not any(c in a or a in c for a in allowed)]
    assert not found, (
        f"{path.relative_to(ROOT)} states a fixed catalog count: {found}. Compute it from the build instead "
        "(src/coffeedata/site.py), or drop it."
    )


def test_the_site_states_single_origin_counts_with_the_public_tables_definition():
    """The site's single-origin numbers are public_market_weekly's, whose filter is the site's LISTED catalog,
    less blends and coffees of unknown origin."""
    sql = (ROOT / "dbt" / "models" / "public" / "public_market_weekly.sql").read_text()
    where = re.sub(r"\s+", " ", sql.split("where", 1)[1].split(")", 1)[0]).strip()
    assert where == f"{site.LISTED} and not is_blend and origin_country is not null"
    assert site.SINGLE_ORIGIN == "public_market_weekly"


@pytest.mark.parametrize(
    ("n", "text"),
    [(19964, "20,000"), (40800, "41,000"), (1845, "1,800"), (15759, "16,000"), (3205, "3,200"), (52, "52")],
)
def test_about_rounds_a_count_for_copy(n, text):
    assert quality.about(n) == text
