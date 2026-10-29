"""The wholesale rules being compared. C0 is the rule before this change (main); C1 is the change."""

import re

C0_TEXT = re.compile(r"\b(wholesale|resale|b2b|food ?service)\b|\bcase (of )?\(?\d+\)?|\bcases? of \d+", re.I)
C0_TAG = re.compile(r"wholesale|resale|b2b|food ?service", re.I)
C1_TEXT = re.compile(
    r"\b(wholesale|whole sale|resale|b2b|food ?service|frac(tion)?[- ]?packs?)\b|\bcase (of )?\(?\d+\)?"
    r"|\bcases? of \d+",
    re.I,
)
C1_TAG = re.compile(r"(wholesale|resale|b2b|food ?service)([-_ ](only|exclusive))?", re.I)
RULES = {"C0": (C0_TEXT, C0_TAG), "C1": (C1_TEXT, C1_TAG)}


def live() -> tuple[re.Pattern, re.Pattern]:
    """The rule the code uses now (coffeedata.classify), which the accuracy floors test."""
    from coffeedata import classify

    return classify._WHOLESALE, classify._WHOLESALE_TAG


# Strata for sampling: what looks like a bulk or cafe listing although C0 doesn't flag it.
BULK = re.compile(
    r"\b(bulk|[2-9]\s*-?\s*(lb|lbs|pound|pounds)\b|\d{2}\s*-?\s*(lb|lbs|pound)|[1-9]\s*kg|kilo|caf[eé]s?|office"
    r"|case|cases|pack of|\d+\s*-?\s*pack|restaurant|foodservice|food service|commercial|trade)\b",
    re.I,
)


def flag(r: dict, name: str) -> bool:
    text, tag = live() if name == "live" else RULES[name]
    return bool(
        text.search(f"{r['title'] or ''} | {r['product_type']}")
        or any(tag.fullmatch(t.strip()) for t in r["tags"])
    )


def looks_bulk(r: dict) -> bool:
    g = [x for x in r["grams"] if x]
    return bool(BULK.search(r["title"] or "")) or (bool(g) and min(g) >= 1800)


def stratum(r: dict) -> str:
    """F: C0 flags it. B: C0 doesn't, but it looks bulk. N: only C1 flags it. R: the rest (not sampled)."""
    return "F" if r["flagged"] else "B" if looks_bulk(r) else "N" if flag(r, "C1") else "R"
