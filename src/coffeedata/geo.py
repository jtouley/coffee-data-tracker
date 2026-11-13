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
    return {"ranges": len(rows), "prefix_accuracy": prefix_accuracy(rows)}


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


def changed_events(sources: list[str], old: Path, new: Path) -> int:
    """How many stored events (bots included) would get another (country, region, city) from `new` than from `old`:
    the number `coffeedata geo-update` prints for the PR that edits the lock. A report, never stored; the location
    itself is derived only in dbt (int_usage_location), with the same encoding (to_hex) and the same range rule."""
    import duckdb

    if not sources:
        return 0
    paths = ", ".join(f"'{s}'" for s in sources)
    con = duckdb.connect()
    try:
        prefixes = con.execute(
            f"select ip_prefix, count(*) from read_parquet([{paths}], union_by_name = true) "
            "where ip_prefix is not null group by ip_prefix"
        ).fetchall()
        rows = []
        for prefix, n in prefixes:
            try:
                rows.append((to_hex(prefix.split("/")[0]), n))
            except ValueError:
                continue
        con.execute("create temp table p (ip_hex varchar, n bigint)")
        con.executemany("insert into p values (?, ?)", rows)

        def place(path: Path) -> str:
            return (
                f"select p.ip_hex, p.n, case when p.ip_hex <= r.ip_end then [r.country, r.region, r.city] end as place "
                f"from p asof left join read_parquet('{path}') as r on p.ip_hex >= r.ip_start"
            )

        row = con.execute(
            f"select coalesce(sum(a.n), 0) from ({place(old)}) as a inner join ({place(new)}) as b using (ip_hex) "
            "where a.place is distinct from b.place"
        ).fetchone()
        return int(row[0]) if row else 0  # an aggregate always returns one row; fetchone() is typed Optional
    finally:
        con.close()
