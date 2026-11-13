"""The pinned IP-to-city database the usage build reads (docs/usage/README.md, design section 5).

Two reference files, prepared once by `coffeedata geo-update` and then never changed:

- `geo/dbip-city-lite-YYYY-MM.parquet`: DB-IP "IP to City Lite" (CC BY 4.0) as a range table, one row per range,
  `ip_start` and `ip_end` as 32 lowercase hex digits of the 128-bit address (an IPv4 address is IPv4-mapped,
  ::ffff:a.b.c.d, so both families sort in one order), then `country` (ISO 3166-1 alpha-2), `region` and `city`.
  The coordinates are dropped.
- `geo/geonames-YYYY-MM-DD.parquet`: GeoNames `cities500` (CC BY 4.0), the places of 50,000 people or more, keyed by
  the names DB-IP uses: `country`, `region` (the admin1 name), `city`, `population`. Smaller places are left out on
  purpose: a city with no row is a small town, and the marts roll it up into "other, <state>".

`config/geo.lock` names each file, its release date and its sha256 (and the source download's). The usage build
copies the locked files from the usage dataset (or finds them in its cache), checks each sha256 before dbt runs, and
passes the paths to dbt; dbt reads exactly those files. Same raw plus same lock gives the same output.

Preparing is not deriving: it reshapes a reference download into a lookup table. Every location an event gets is
derived in dbt (int_usage_location), after raw.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import ipaddress
import re
import tomllib
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

LOCK = Path(__file__).resolve().parents[2] / "config" / "geo.lock"
# The two files' keys in the lock, and what dbt calls them (var('geo_db'), var('geo_cities')).
DBIP, CITIES = "dbip_city_lite", "geonames_cities"
DBT_VARS = {DBIP: "geo_db", CITIES: "geo_cities"}
# Places this size or larger are kept in the city table: the marts never name a smaller one (design section 5).
MIN_POPULATION = 50_000
V4_MAPPED = 0xFFFF << 32  # ::ffff:0:0/96
# Where the downloads come from, recorded in the lock (`coffeedata geo-update`).
DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.csv.gz"
CITIES_URL = "https://download.geonames.org/export/dump/cities500.zip"
ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"
RANGE_SCHEMA = pa.schema(
    [("ip_start", pa.string()), ("ip_end", pa.string()), ("country", pa.string()), ("region", pa.string()),
     ("city", pa.string())]
)  # fmt: skip
CITY_SCHEMA = pa.schema(
    [("country", pa.string()), ("region", pa.string()), ("city", pa.string()), ("population", pa.int64())]
)
# Fixed writer settings, so one input and one pyarrow version always give the same bytes (and so the same sha256).
_WRITE = {
    "compression": "zstd",
    "row_group_size": 1_000_000,
    "write_statistics": True,
    "use_dictionary": True,
}


def to_hex(ip: str) -> str:
    """An address as the 32 hex digits int_usage_location compares; IPv4 is IPv4-mapped."""
    a = ipaddress.ip_address(ip)
    n = V4_MAPPED | int(a) if a.version == 4 else int(a)
    return f"{n:032x}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dbip_rows(lines: Iterable[str]) -> Iterator[tuple[int, int, str | None, str | None, str | None]]:
    """(start, end, country, region, city) per CSV row, IPv6 ranges inside the IPv4-mapped space left out (they
    would overlap the IPv4 rows there)."""
    for row in csv.reader(lines):
        start, end, _continent, country, region, city = row[:6]
        a, b = ipaddress.ip_address(start), ipaddress.ip_address(end)
        if a.version == 4:
            s, e = V4_MAPPED | int(a), V4_MAPPED | int(b)
        else:
            s, e = int(a), int(b)
            if s <= (V4_MAPPED | 0xFFFFFFFF) and e >= V4_MAPPED:
                continue
        yield s, e, (country if country not in ("", "ZZ") else None), (region or None), (city or None)


def prepare_dbip(csv_gz: Path, out: Path) -> dict:
    """DB-IP's CSV download (gzip) to the range table. Returns counts and the prefix measurement."""
    with gzip.open(csv_gz, "rt", encoding="utf-8", newline="") as f:
        rows = sorted(_dbip_rows(f))
    for (_, e1, *_), (s2, *_) in zip(rows, rows[1:], strict=False):
        if s2 <= e1:
            raise ValueError("DB-IP ranges overlap; refusing to prepare an ambiguous lookup")
    table = pa.table(
        {
            "ip_start": [f"{r[0]:032x}" for r in rows],
            "ip_end": [f"{r[1]:032x}" for r in rows],
            "country": [r[2] for r in rows],
            "region": [r[3] for r in rows],
            "city": [r[4] for r in rows],
        },
        schema=RANGE_SCHEMA,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out, **_WRITE)
    return {
        "ranges": len(rows),
        "prefix_accuracy": prefix_accuracy(rows),
        "consensus": consensus_levels(rows),
    }


def prefix_accuracy(rows: list[tuple[int, int, str | None, str | None, str | None]]) -> dict:
    """What masking costs on this file, for IPv4 /24 and IPv6 /40 and /48 (design sections 5 and 10).

    - `blocks_with_more_than_one_place`: blocks the file's rows give more than one place (country, region, city).
    - `ranges_moved`: rows whose masked address (their first address with the host bits zeroed) looks up another
      place than the row's own, of `ranges` rows in the family.
    - `addresses_moved_share`: of the addresses the family's rows cover, the share whose masked lookup gives another
      place (or none) than the full address: the share of events that would land in another city.

    `rows` sorted and disjoint, as prepare_dbip makes them."""
    import bisect

    out = {}
    for name, version, bits in (("ipv4_24", 4, 24), ("ipv6_40", 6, 40), ("ipv6_48", 6, 48)):
        fam = [r for r in rows if (V4_MAPPED <= r[0] <= V4_MAPPED | 0xFFFFFFFF) == (version == 4)]
        starts = [r[0] for r in fam]
        shift = (32 if version == 4 else 128) - bits
        mask = (1 << shift) - 1
        places: dict[int, set] = {}
        moved_ranges = moved_addresses = total = 0
        for s, e, *place in fam:
            total += e - s + 1
            first, last = s >> shift, e >> shift
            if s & mask:
                places.setdefault(first, set()).add(tuple(place))
            if e & mask != mask:
                places.setdefault(last, set()).add(tuple(place))
            if s & mask:
                net = s & ~mask
                i = bisect.bisect_right(starts, net) - 1
                found = tuple(fam[i][2:]) if i >= 0 and fam[i][1] >= net else None
                if found != tuple(place):
                    moved_ranges += 1
                    moved_addresses += min(e, s | mask) - s + 1
        out[name] = {
            "blocks_with_more_than_one_place": sum(1 for p in places.values() if len(p) > 1),
            "ranges": len(fam),
            "ranges_moved": moved_ranges,
            "addresses_moved_share": round(moved_addresses / total, 6) if total else 0.0,
        }
    return out


LEVELS = ("city", "region", "country", "unknown")
# DB-IP names some places "City (Neighbourhood)"; the lookup keeps the base name only (int_usage_location does the
# same in SQL), so they match GeoNames and the neighbourhood is never shown.
_NEIGHBOURHOOD = re.compile(r"\s*\(.*\)\s*$")


def base_city(name: str | None) -> str | None:
    return _NEIGHBOURHOOD.sub("", name) if name else name


def _level(places: set[tuple]) -> str:
    """The most precise level every place in a block agrees on (int_usage_location's rule): city, region, country,
    else unknown. A place is (country, region, city) with the city's base name."""
    countries = {p[0] for p in places}
    if len(countries) != 1 or None in countries:
        return "unknown"
    regions = {p[1] for p in places}
    if len(regions) != 1 or None in regions:
        return "country"
    cities = {p[2] for p in places}
    return "city" if len(cities) == 1 and None not in cities else "region"


def consensus_levels(rows: list[tuple[int, int, str | None, str | None, str | None]]) -> dict:
    """How the masked lookup resolves on this file, for IPv4 /24 and IPv6 /40 (the collector's masks): for every block
    the file's rows touch, the level all the rows inside it agree on. `shared` counts blocks two or more rows share
    (where masking can lose precision); `all` adds the blocks one row covers whole, which take that row's level.
    Shares of blocks, not of traffic. `rows` sorted and disjoint."""
    out = {}
    for name, version, bits in (("ipv4_24", 4, 24), ("ipv6_40", 6, 40)):
        shift = (32 if version == 4 else 128) - bits
        mask = (1 << shift) - 1
        partial: dict[int, set] = {}
        whole = dict.fromkeys(LEVELS, 0)
        for s, e, country, region, city in rows:
            if (V4_MAPPED <= s <= V4_MAPPED | 0xFFFFFFFF) != (version == 4):
                continue
            lo, hi = (s & 0xFFFFFFFF, e & 0xFFFFFFFF) if version == 4 else (s, e)
            place = (country, region, base_city(city))
            first, last = lo >> shift, hi >> shift
            first_whole = first if lo & mask == 0 else first + 1
            last_whole = last if hi & mask == mask else last - 1
            if first_whole <= last_whole:
                whole[_level({place})] += last_whole - first_whole + 1
            if lo & mask:
                partial.setdefault(first, set()).add(place)
            if hi & mask != mask:
                partial.setdefault(last, set()).add(place)
        shared = dict.fromkeys(LEVELS, 0)
        for places in partial.values():
            shared[_level(places)] += 1
        n_shared, n_all = len(partial), len(partial) + sum(whole.values())
        out[name] = {
            "shared": {k: round(v / n_shared, 4) if n_shared else 0.0 for k, v in shared.items()},
            "shared_blocks": n_shared,
            "all": {k: round((v + whole[k]) / n_all, 4) if n_all else 0.0 for k, v in shared.items()},
            "all_blocks": n_all,
        }
    return out


def read_ranges(path: Path) -> list[tuple[int, int, str | None, str | None, str | None]]:
    """A prepared range table back as sorted (start, end, country, region, city) rows, for measuring."""
    t = pq.read_table(path).to_pydict()
    return [
        (int(a, 16), int(b, 16), c, r, x)
        for a, b, c, r, x in zip(
            t["ip_start"], t["ip_end"], t["country"], t["region"], t["city"], strict=True
        )
    ]


def _admin1(text: str) -> dict[str, str]:
    """GeoNames admin1CodesASCII.txt: 'US.CO' -> 'Colorado'."""
    names = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            names[parts[0]] = parts[1]
    return names


def prepare_cities(cities_zip: Path, admin1_txt: Path, out: Path) -> dict:
    """GeoNames cities500 to the city table: every name a place of MIN_POPULATION or more goes by (its name, its
    ASCII name, and an alternate name no other place in its region has as its own name), with the largest population
    when two places share one. Returns counts."""
    regions = _admin1(admin1_txt.read_text(encoding="utf-8"))
    with zipfile.ZipFile(cities_zip) as z:
        text = z.read("cities500.txt").decode("utf-8")
    places = []
    own_names: set[tuple[str, str, str]] = set()
    for line in io.StringIO(text):
        f = line.rstrip("\n").split("\t")
        if len(f) < 15:
            continue
        country, region = f[8], regions.get(f"{f[8]}.{f[10]}")
        if not region:
            continue
        pop = int(f[14] or 0)
        names = {f[1], f[2]}
        own_names |= {(country, region, n.lower()) for n in names}
        places.append((country, region, names, [a for a in f[3].split(",") if a], pop))
    best: dict[tuple[str, str, str], tuple[str, int]] = {}
    for country, region, names, alternates, pop in places:
        if pop < MIN_POPULATION:
            continue
        keep = set(names) | {a for a in alternates if (country, region, a.lower()) not in own_names}
        for n in keep:
            key = (country, region, n.lower())
            if key not in best or pop > best[key][1]:
                best[key] = (n, pop)
    rows = sorted((c, r, low, pop) for (c, r, low), (_n, pop) in best.items())
    table = pa.table(
        {"country": [r[0] for r in rows], "region": [r[1] for r in rows], "city": [r[2] for r in rows],
         "population": [r[3] for r in rows]},
        schema=CITY_SCHEMA,
    )  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out, **_WRITE)
    return {"names": len(rows)}


def read_lock(path: Path = LOCK) -> dict[str, dict]:
    lock = tomllib.loads(path.read_text())
    for key in (DBIP, CITIES):
        entry = lock.get(key) or {}
        for field in ("file", "release", "sha256"):
            if not entry.get(field):
                raise ValueError(f"{path}: [{key}] has no {field}")
        if not entry["file"].startswith("geo/") or ".." in entry["file"]:
            raise ValueError(f"{path}: [{key}] file must be under geo/ in the usage dataset")
    return lock


def render_lock(entries: dict[str, dict]) -> str:
    """The lock file's text, in a fixed order."""
    out = ["# The pinned geo files the usage build reads (src/coffeedata/geo.py, docs/usage/README.md).",
           "# Changed only by a reviewed PR; `coffeedata geo-update` writes it.", ""]  # fmt: skip
    for key in (DBIP, CITIES):
        out.append(f"[{key}]")
        for field in (
            "file",
            "release",
            "sha256",
            "source",
            "source_sha256",
            "admin1_source",
            "admin1_sha256",
            "licence",
            "attribution",
        ):
            if field in entries[key]:
                out.append(f'{field} = "{entries[key][field]}"')
        out.append("")
    return "\n".join(out)


def verify(lock: dict[str, dict], local: dict[str, Path]) -> None:
    """Each locked file's sha256, before dbt reads it: ValueError on a file that isn't the locked one."""
    for key in (DBIP, CITIES):
        got = sha256(local[key])
        if got != lock[key]["sha256"]:
            raise ValueError(
                f"{lock[key]['file']}: sha256 {got} is not the locked {lock[key]['sha256']} (config/geo.lock)"
            )


def locate(lock: dict[str, dict], cache: Path, fetch=None) -> dict[str, Path]:
    """The locked files on disk: from the cache (cache/<file>), else fetched there by `fetch(file, dest)` (the usage
    dataset, read-only), then checked against the lock. ValueError when a file is missing or differs."""
    local = {}
    for key in (DBIP, CITIES):
        dest = cache / lock[key]["file"]
        if not dest.is_file():
            if fetch is None:
                raise ValueError(
                    f"{lock[key]['file']} is not in {cache} and there is nowhere to fetch it from"
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            fetch(lock[key]["file"], dest)
        local[key] = dest
    verify(lock, local)
    return local


def block(prefix: str) -> tuple[str, str] | None:
    """A stored ip_prefix's block as (first, last) 32-hex-digit addresses, the way int_usage_location reads it: the
    length counted in the 128-bit space and rounded down to a whole hex digit. None for anything else."""
    try:
        net = ipaddress.ip_network(prefix.strip(), strict=False)
    except ValueError:
        return None
    keep = (net.prefixlen + (96 if net.version == 4 else 0)) // 4
    start = to_hex(str(net.network_address))
    return start, start[:keep] + "f" * (32 - keep)


def placed(places: set[tuple]) -> tuple | None:
    """int_usage_location's consensus over the places of a block's rows: (country, region, city) down to the level
    they all agree on, None above it."""
    level = _level(places) if places else "unknown"
    if level == "unknown":
        return None
    country, region, city = next(iter(places))
    return (country, region if level in ("region", "city") else None, city if level == "city" else None)


def changed_events(sources: list[str], old: Path, new: Path) -> int:
    """How many stored events (bots included) would be placed differently by `new` than by `old`: the number
    `coffeedata geo-update` prints for the PR that edits the lock. A report, never stored; the location itself is
    derived only in dbt (int_usage_location), and this follows its rule: every row in the prefix's block, the level
    they all agree on, neighbourhood names cut."""
    import duckdb

    if not sources:
        return 0
    paths = ", ".join(f"'{s}'" for s in sources)
    con = duckdb.connect()
    try:
        if any(s.startswith("hf://") for s in sources):
            # the private usage dataset: the Hub token from the environment or the local login, never printed
            con.execute("create secret (type huggingface, provider credential_chain)")
        prefixes = con.execute(
            f"select ip_prefix, count(*) from read_parquet([{paths}], union_by_name = true) "
            "where ip_prefix is not null group by ip_prefix"
        ).fetchall()
        blocks = [(span[0], span[1], n) for prefix, n in prefixes if (span := block(prefix))]
        con.execute("create temp table b (start_hex varchar, end_hex varchar, n bigint)")
        con.executemany("insert into b values (?, ?, ?)", blocks)

        def places(path: Path) -> dict[str, tuple | None]:
            found: dict[str, set] = {s: set() for s, _e, _n in blocks}
            for start, country, region, city in con.execute(
                f"select b.start_hex, r.country, r.region, r.city from b inner join read_parquet('{path}') as r "
                "on r.ip_start <= b.end_hex and r.ip_end >= b.start_hex"
            ).fetchall():
                found[start].add((country, region, base_city(city) or None))
            return {s: placed(p) for s, p in found.items()}

        a, b = places(old), places(new)
        return sum(n for s, _e, n in blocks if a[s] != b[s])
    finally:
        con.close()
