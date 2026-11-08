"""The Sources page: every roaster the registry lists (the served release's dim_roaster: the curated seed, issue
forms, discovery's roasteries), each inheriting the roaster flag, and discovery's whole source list (lake
`candidates`: every place a discovery run read, roasters and cafés), one row per place, with the owner's flag on it. Copied once per discovery run and served release into a local
DuckDB file and searched there, like the Catalog. Nothing here fetches any site: the hints are what discovery
recorded (the place's tag, "roast" in its name), and a hint never flags anything."""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import duckdb
import pyarrow as pa

from coffeedata import quality
from coffeedata.lake import Lake, no_files
from coffeedata.registry import normalize_domain

from .catalog import FACET_VALUES, PAGE, Filters, _one
from .queries import safe_url, store_url

CACHE_VERSION = "v1"
EMPTY = ("(select null::timestamptz as added_at, null::varchar as source, null::varchar as roaster_id, "
         "null::varchar as name, null::varchar as city, null::varchar as state, null::varchar as website, "
         "null::varchar[] as tags, null::boolean as roast_in_name where false)")  # fmt: skip


# The Filter / Sort panel's orders: the first is the default (the places to review first, those with a hint first).
SORTS = {
    "status": ("Status, to review first", "status = 'unreviewed' desc, hint is null, name, roaster_id"),
    "name": ("Name", "name, roaster_id"),
    "state": ("State, then city", "state nulls last, city nulls last, name, roaster_id"),
    "newest": ("Newest listed", "first_seen desc nulls last, name, roaster_id"),
}


@dataclass
class SourceFilters(Filters):
    FACETS = ("status", "origin", "tag", "state", "city")
    COLUMN = {f: f for f in FACETS}
    SEARCH = ("name", "roaster_id", "coalesce(city, '')")
    PRICES = False
    sort: str = "status"

    @classmethod
    def from_query(cls, params) -> SourceFilters:
        """A pasted address finds its place: `https://www.example.com/shop` searches for `example.com`."""
        f = cast(SourceFilters, super().from_query(params))
        if "://" in f.q or f.q.lower().startswith("www."):
            f.q = normalize_domain(f.q)
        f.sort = s if (s := params.get("sort")) in SORTS else "status"
        return f

    def in_use(self) -> int:
        """How many filters and orders the panel has applied (the button's count)."""
        return len(self.values) + (self.sort != "status")


class SourceList:
    """One local DuckDB file per discovery run and served release, made on first use."""

    def __init__(self, lake: Lake, cache_dir: Path):
        self.lake, self.dir = lake, Path(cache_dir)
        self.lock = threading.Lock()

    def _files(self, con) -> list[str]:
        """The candidates files, from one listing per source (no read of them): the copy's key and its input."""
        files: list[str] = []
        for pattern in self.lake.sources("candidates"):
            try:
                files += [r[0] for r in con.execute("select file from glob(?)", [pattern]).fetchall()]
            except Exception as e:  # noqa: BLE001 - duckdb.IOException; the table has no files yet
                if not (no_files(e) or "HTTP 404" in str(e)):
                    raise
        return sorted(files)

    def ensure(self, served: str | None) -> Path:
        con = self.lake.connect()
        try:
            files = self._files(con)
        finally:
            con.close()
        key = hashlib.sha256(f"{CACHE_VERSION}|{'|'.join(files)}|{served}".encode()).hexdigest()[:16]
        path = self.dir / f"sources-{key}.duckdb"
        with self.lock:
            if path.exists():
                os.utime(path)
            else:
                self._make(path, served, files)
        return path

    def _make(self, path: Path, served: str | None, files: list[str]) -> None:
        """places: one row per domain of the source list, its latest record, when it was first and last listed,
        whether the latest run added it (new) or dropped it (gone), and whether the served release lists it."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.unlink(missing_ok=True)
        roasters = (quality.release_files(self.lake, served) if served else {}).get("dim_roaster")
        con = self.lake.connect(str(tmp))
        try:
            listed = ", ".join("'" + f.replace("'", "''") + "'" for f in files)
            scan = (
                f"read_parquet([{listed}], union_by_name = true, hive_partitioning = false)"
                if files
                else EMPTY
            )
            con.execute(f"create table c as select * from {scan} where roaster_id is not null")
            latest = _one(con, "select max(added_at)::varchar from c")
            reg = (f"select roaster_id, roaster_name as name, city, state, sources from read_parquet("
                   f"'{roasters.replace(chr(39), chr(39) * 2)}', hive_partitioning = false)" if roasters else
                   "select null::varchar as roaster_id, null::varchar as name, null::varchar as city, "
                   "null::varchar as state, null::varchar as sources where false")  # fmt: skip
            con.execute(f"""
                create table places as
                with runs as (select distinct added_at from c),
                last2 as (select max(added_at) as latest,
                                 (select max(added_at) from runs where added_at < (select max(added_at) from runs))
                                     as previous from runs),
                cand as (
                    select l.*, s.first_seen, s.last_seen from (
                        select * from c qualify row_number() over (partition by roaster_id
                                                                   order by added_at desc, source) = 1) l
                    join (select roaster_id, min(added_at) as first_seen, max(added_at) as last_seen
                          from c group by roaster_id) s using (roaster_id)),
                reg as ({reg})
                select coalesce(k.roaster_id, g.roaster_id) as roaster_id, coalesce(g.name, k.name) as name,
                       coalesce(g.city, k.city) as city, coalesce(g.state, k.state) as state, k.website,
                       case when list_contains(k.tags, 'coffee_roastery') then 'coffee_roastery'
                            when list_contains(k.tags, 'coffee_shop') then 'coffee_shop'
                            when list_contains(k.tags, 'cafe') then 'cafe' end as tag,
                       if(k.roast_in_name, 'roast in the name', null) as hint,
                       -- where it came from: the registry's sources (the seed's curated or manual, discovery's
                       -- overture or osm, ...) for a registry roaster, else the discovery run's; origin, the kinds
                       coalesce(g.sources, k.source) as sources,
                       array_to_string(list_sort(list_distinct(list_transform(
                           string_split(coalesce(g.sources, k.source), '+'),
                           lambda x: regexp_replace(x, '-[0-9].*$', '')))), '+') as origin,
                       k.first_seen, k.last_seen,
                       case when k.first_seen = r.latest and r.previous is not null then 'new'
                            when k.last_seen < r.latest and k.last_seen >= r.previous then 'gone' end as change,
                       g.roaster_id is not null as in_registry
                from cand k full outer join reg g using (roaster_id)
                cross join last2 r""")
            con.execute("drop table c")
            con.execute("create table meta as select ? as latest_run, ? as served", [latest, served])
            con.execute("checkpoint")
        finally:
            con.close()
        os.replace(tmp, path)
        for p in sorted(self.dir.glob("sources-*.duckdb"), key=lambda p: p.stat().st_mtime, reverse=True)[2:]:
            if p != path:
                p.unlink(missing_ok=True)

    def search(self, served: str | None, f: SourceFilters, flags: dict[str, dict] | None) -> dict:
        """The places matching `f` (a page of them), counts by status and of the latest run's changes, and each
        facet's values. `flags`: the control plane's latest flag per place; None when it couldn't be read, and then
        no place has a status (an inherited or unreviewed label could be wrong)."""
        known = flags is not None
        if flags is None:
            flags = {}
            f.values.pop("status", None)
        path = self.ensure(served)
        con = duckdb.connect(str(path), read_only=True)
        try:
            con.register("flags", pa.table({
                "roaster_id": list(flags), "flag": [v["flag"] for v in flags.values()],
                "batch_id": [v["batch_id"] for v in flags.values()],
            }, schema=pa.schema([("roaster_id", pa.string()), ("flag", pa.string()), ("batch_id", pa.string())])))  # fmt: skip
            # Every registry roaster inherits the roaster flag; an explicit flag, either way, wins.
            status = ("""case when f.flag = 'not_roaster' then 'not a roaster' when f.flag = 'roaster' then 'roaster'
                                  when p.in_registry then 'roaster (inherited)' else 'unreviewed' end"""
                      if known else "null::varchar")  # fmt: skip
            view = f"(select p.*, f.batch_id, {status} as status from places p left join flags f using (roaster_id))"
            where, params = f.where()
            total = _one(con, f"select count(*) from {view}")
            matching = _one(con, f"select count(*) from {view} {where}", params)
            cur = con.execute(
                f"""select roaster_id, name, city, state, website, tag, hint, sources, status, change, batch_id,
                           in_registry, first_seen, last_seen from {view} {where}
                    order by {SORTS[f.sort][1]}
                    limit {PAGE} offset {(f.page - 1) * PAGE}""",
                params,
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
            for r in rows:
                r["url"] = safe_url(r["website"]) or store_url(r["roaster_id"])
            facets = {}
            for name in SourceFilters.FACETS:
                w, p = f.where(skip=name)
                facets[name] = con.execute(
                    f"""select coalesce({name}::varchar, '(none)'), count(*) as n from {view} {w}
                        group by 1 order by n desc, 1 limit {FACET_VALUES}""",
                    p,
                ).fetchall()
            counts = {k: dict(con.execute(f"select coalesce({k}, ''), count(*) from {view} group by 1").fetchall())
                      for k in ("status", "change")}  # fmt: skip
            latest_run, served_build = con.execute("select * from meta").fetchone() or (None, None)
            meta = {"latest_run": latest_run, "served": served_build}
            return {"total": total, "matching": matching, "rows": rows, "facets": facets, "counts": counts,
                    "meta": meta, "page": f.page, "pages": max(1, -(-matching // PAGE))}  # fmt: skip
        finally:
            con.close()
