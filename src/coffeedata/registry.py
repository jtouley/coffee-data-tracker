"""The roaster registry: config/roasters.csv, one row per roaster website."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .models import Roaster

REGISTRY = Path("config/roasters.csv")
FIELDS = list(Roaster.model_fields)
EXTRA = ["coffees", "failures", "last_ingested"]

# Hosts that are never a roaster's own storefront.
NOT_A_ROASTER_SITE = re.compile(
    r"(^|\.)(facebook|instagram|yelp|linktr|toasttab|google|business\.site|square\.site|squareup|"
    r"order\.online|doordash|ubereats|grubhub|tiktok|twitter|x|linkedin|tripadvisor|mapquest|youtube|"
    r"clover|wixsite|godaddysites|weebly|blogspot|wordpress)\.(com|ee|site|online)$"
)


def normalize_domain(d: str) -> str:
    d = d.strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0]
    return re.sub(r"^(www\d?|locations|shop|store)\.", "", d)


def slugify_domain(domain: str, taken: set[str]) -> str:
    base = re.sub(r"\.(com|co|coffee|net|org|us|shop|store|cafe|io|biz|info|coop)$", "", domain)
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "roaster"
    s, i = slug, 2
    while s in taken:
        s, i = f"{slug}-{i}", i + 1
    taken.add(s)
    return s


def load(path: Path = REGISTRY) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def roasters(rows: list[dict]) -> list[Roaster]:
    out = []
    for r in rows:
        clean = {k: v for k, v in r.items() if k in FIELDS and v not in (None, "")}
        clean["locations"] = int(clean.get("locations") or 1)
        out.append(Roaster(**clean))
    return out


def save(rows: list[dict], path: Path = REGISTRY) -> None:
    cols = FIELDS + [c for c in EXTRA if any(c in r for r in rows)]
    rows = sorted(rows, key=lambda r: r["domain"])
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in cols} for r in rows)


def merge(rows: list[dict], candidates: list[dict]) -> tuple[list[dict], int]:
    """Union new discovery candidates into the registry by domain. Returns (rows, added)."""
    by_domain = {r["domain"]: r for r in rows}
    taken = {r["slug"] for r in rows}
    added = 0
    for c in candidates:
        d = normalize_domain(c.get("domain", ""))
        if not d or "." not in d or NOT_A_ROASTER_SITE.search(d):
            continue
        if d in by_domain:
            r = by_domain[d]
            srcs = set(filter(None, r.get("sources", "").split("+"))) | {c.get("source", "")}
            r["sources"] = "+".join(sorted(filter(None, srcs)))
            continue
        by_domain[d] = {
            "slug": slugify_domain(d, taken), "name": c.get("name", ""), "domain": d,
            "city": c.get("city", ""), "state": c.get("state", ""), "sources": c.get("source", ""),
            "locations": c.get("locations") or 1, "confidence": c.get("confidence", ""),
            "tagged_roastery": c.get("tagged_roastery", ""), "roast_in_name": c.get("roast_in_name", ""),
            "is_chain": "false", "status": "pending",
        }
        added += 1
    return list(by_domain.values()), added
