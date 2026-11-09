"""The Catalog page: every product of a build (ops product_decisions) with its coffee attributes (the release's
dim_coffee), copied once per build_id into a local DuckDB file and searched there. Releases are immutable, so a
build's file never goes stale; a new build gets a new file. The file lives in the admin's cache folder, never in the
lake."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import duckdb
import pyarrow as pa

from coffeedata import classify, products, quality
from coffeedata.classify import KeywordFilter
from coffeedata.lake import Lake

from .queries import safe_url

FACETS = ("roaster", "origin", "process", "coffee", "classified_by", "listed", "stock")
COLUMN = {
    "roaster": "roaster_id",
    "origin": "origin_country",
    "process": "process",
    "coffee": "coffee",
    "classified_by": "classified_by",
    "listed": "listed",
    "stock": "stock",
}
PAGE = 100
# The products table's shape: a new version makes new files (v2: input_hash, platform; v3: tags; v4: the keyword
# filters' normalized text and unfiltered_is_coffee).
CACHE_VERSION = "v4"
FACET_VALUES = 40
BUILD_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")


@dataclass
class Filters:
    """A page's search, facets, price range and page number, from its query string; where() is the SQL over the
    page's table. The Sources page uses the same with its own facets (sources.SourceFilters)."""

    FACETS: ClassVar[tuple[str, ...]] = FACETS
    COLUMN = COLUMN
    SEARCH: ClassVar[tuple[str, ...]] = (
        "title",
        "roaster_id",
        "product_id",
        "coalesce(url, '')",
        "coalesce(coffee_name, '')",
    )
    PRICES = True
    q: str = ""
    values: dict[str, str] = field(default_factory=dict)  # facet -> value
    price_min: float | None = None
    price_max: float | None = None
    page: int = 1

    @classmethod
    def from_query(cls, params) -> Filters:
        def num(name: str) -> float | None:
            try:
                v = float(params.get(name) or "")
            except ValueError:
                return None
            return v if 0 <= v < 1e6 else None

        try:
            page = max(1, min(int(params.get("page") or 1), 10_000))
        except ValueError:
            page = 1
        return cls(
            q=(params.get("q") or "").strip()[:200],
            values={f: v[:200] for f in cls.FACETS if (v := (params.get(f) or "").strip())},
            price_min=num("price_min") if cls.PRICES else None,
            price_max=num("price_max") if cls.PRICES else None,
            page=page,
        )

    def where(self, skip: str | None = None) -> tuple[str, list[object]]:
        sql: list[str] = []
        params: list[object] = []
        if self.q:
            sql.append("(" + " or ".join(f"{c} ilike ? escape '\\'" for c in self.SEARCH) + ")")
            like = "%" + self.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like] * len(self.SEARCH)
        for f, v in self.values.items():
            if f == skip:
                continue
            if v == "(none)":
                sql.append(f"{self.COLUMN[f]} is null")
            else:
                sql.append(f"{self.COLUMN[f]} = ?")
                params.append(v)
        if self.price_min is not None:
            sql.append("min_price_usd >= ?")
            params.append(self.price_min)
        if self.price_max is not None:
            sql.append("min_price_usd <= ?")
            params.append(self.price_max)
        return (" where " + " and ".join(sql)) if sql else "", params

    def chips(self) -> list[tuple[str, str]]:
        out = [("q", f"“{self.q}”")] if self.q else []
        out += [(f, f"{f.replace('_', ' ')}: {v}") for f, v in self.values.items()]
        if self.price_min is not None:
            out.append(("price_min", f"from ${self.price_min:g}"))
        if self.price_max is not None:
            out.append(("price_max", f"to ${self.price_max:g}"))
        return out


def _one(con, sql: str, params: list | None = None) -> Any:
    row = con.execute(sql, params or []).fetchone()
    return row[0] if row else None


class Catalog:
    """One local DuckDB file per build, made on first use (a lock per build, so two requests make it once)."""

    def __init__(self, lake: Lake, cache_dir: Path):
        self.lake, self.dir = lake, Path(cache_dir)
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.Lock()

    def path(self, build_id: str) -> Path:
        if not BUILD_ID.match(build_id):
            raise ValueError("bad build id")
        return self.dir / f"catalog-{CACHE_VERSION}-{build_id}.duckdb"

    def ensure(self, build_id: str) -> dict:
        """The build's catalog file, made if missing. Returns what it was made from."""
        path = self.path(build_id)
        with self.guard:
            lock = self.locks.setdefault(build_id, threading.Lock())
        with lock:
            if path.exists():
                os.utime(path)  # used now: the newest, so pruning keeps it
            else:
                self._make(build_id, path)
        con = duckdb.connect(str(path), read_only=True)
        try:
            return dict(con.execute("select key, value from meta").fetchall())
        finally:
            con.close()

    def _make(self, build_id: str, path: Path) -> None:
        ops = quality.ops_files(self.lake, build_id)
        rel = quality.release_files(self.lake, build_id)
        if "product_decisions" not in ops and "dim_coffee" not in rel:
            raise FileNotFoundError(f"build {build_id} has neither product_decisions nor dim_coffee")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.unlink(missing_ok=True)
        con = self.lake.connect(str(tmp))
        try:

            def src(f: str) -> str:
                return f"read_parquet('{f.replace(chr(39), chr(39) * 2)}', hive_partitioning = false)"

            if "dim_coffee" in rel:
                con.execute(f"create table dim as select * from {src(rel['dim_coffee'])}")
            else:
                con.execute(
                    "create table dim (coffee_id varchar, title varchar, origin_country varchar, process varchar, "
                    "in_stock boolean, min_price_usd double, price_per_100g double, is_listed boolean, "
                    "roaster_id varchar, url varchar)"
                )
            dim_cols = {c for (c,) in con.execute("select column_name from (describe dim)").fetchall()}
            pd_tags, pd_unfiltered = "null::varchar[]", "null::boolean"

            def dim(col: str, typ: str) -> str:
                return f"d.{col}" if col in dim_cols else f"null::{typ}"

            if "product_decisions" in ops:
                con.execute(f"create table pd as select * from {src(ops['product_decisions'])}")
                pd_cols = {c for (c,) in con.execute("select column_name from (describe pd)").fetchall()}
                # tags: written from the keyword filters' change on; an older build has none (null: unknown)
                pd_tags = "p.tags" if "tags" in pd_cols else pd_tags
                pd_unfiltered = (
                    "p.unfiltered_is_coffee" if "unfiltered_is_coffee" in pd_cols else pd_unfiltered
                )
                base = f"""
                    select p.coffee_id, p.roaster_id, p.product_id, p.title, p.product_type, p.url,
                           p.first_seen, p.last_seen, p.is_listed, p.is_coffee, p.classified_by, p.verdict_model,
                           p.verdict_label, p.reason, p.input_hash, {pd_tags} as tags,
                           {pd_unfiltered} as unfiltered_is_coffee,
                           {dim("platform", "varchar")} as platform,
                           {dim("title", "varchar")} as coffee_name,
                           {dim("origin_country", "varchar")} as origin_country, {dim("process", "varchar")} as process,
                           {dim("in_stock", "boolean")} as in_stock, {dim("min_price_usd", "double")} as min_price_usd,
                           {dim("price_per_100g", "double")} as price_per_100g, d.coffee_id is not null as in_dim_coffee
                    from pd p left join dim d using (coffee_id)"""
                made_from = "product_decisions + dim_coffee"
            else:  # a build from before the ops tables: its coffees only
                base = f"""
                    select coffee_id, roaster_id, null::varchar as product_id, title, null::varchar as product_type,
                           url, null::date as first_seen, null::date as last_seen, is_listed, true as is_coffee,
                           {dim("classified_by", "varchar")} as classified_by, null::varchar as verdict_model,
                           null::varchar as verdict_label, null::varchar as reason, null::varchar as input_hash,
                           null::varchar[] as tags, null::boolean as unfiltered_is_coffee,
                           {dim("platform", "varchar")} as platform, title as coffee_name,
                           origin_country, process, in_stock, min_price_usd, price_per_100g, true as in_dim_coffee
                    from dim d"""
                made_from = "dim_coffee only (this build has no product_decisions)"
            # The text a keyword filter reads (classify.keyword_text: NFKC, case-folded), computed in Python by the
            # filters' own function and stored, so the preview's SQL narrowing is a superset of what a filter matches;
            # KeywordFilter.matches then decides.
            con.execute(f"create table base as {base}")
            src_rows = con.execute("select coffee_id, title, product_type, tags from base").fetchall()
            kf = pa.table({
                "coffee_id": [r[0] for r in src_rows],
                "kf_title": [classify.keyword_text(r[1]) for r in src_rows],
                "kf_type": [classify.keyword_text(r[2]) for r in src_rows],
                "kf_tags": [classify.keyword_text(" | ".join(r[3] or [])) for r in src_rows],
            })  # fmt: skip
            con.register("kf_text", kf)
            con.execute(
                """create table products as
                    select b.*, k.kf_title, k.kf_type, k.kf_tags, if(is_coffee, 'coffee', 'not coffee') as coffee,
                           if(coalesce(is_listed, false) and coalesce(is_coffee, false), 'listed', 'removed') as listed,
                           case when in_stock then 'in stock' when in_stock is not null then 'out of stock' end as stock
                    from base b left join kf_text k using (coffee_id)"""
            )
            con.unregister("kf_text")
            con.execute("drop table base")
            con.execute("create table meta (key varchar, value varchar)")
            n = con.execute("select count(*) from products").fetchone()[0]
            has_tags = pd_tags == "p.tags"
            con.executemany(
                "insert into meta values (?, ?)",
                [["build_id", build_id], ["made_from", made_from], ["products", str(n)],
                 ["tags", "yes" if has_tags else "no"]],
            )  # fmt: skip
            con.execute("drop table dim")
            con.execute("drop table if exists pd")
            con.execute("checkpoint")
        finally:
            con.close()
        os.replace(tmp, path)
        self._prune(keep=path)

    def _prune(self, keep: Path, files: int = 4) -> None:
        """Keep the few most recently used builds' files (each use touches its file): the cache is on the
        instance's in-memory disk."""
        old = sorted(self.dir.glob("catalog-*.duckdb"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in old[files:]:
            if p != keep:
                p.unlink(missing_ok=True)

    def members(self, build_id: str, coffee_ids: list[str], hashes: list[str]) -> list[dict]:
        """The build's products with these coffee ids or versions (input_hash), with their roaster and platform: who a
        decision by version covers (every product sharing it, across roasters)."""
        if not (coffee_ids or hashes):
            return []
        self.ensure(build_id)
        con = duckdb.connect(str(self.path(build_id)), read_only=True)
        try:
            rows = con.execute(
                "select coffee_id, input_hash, roaster_id, platform from products "
                "where coffee_id in (select unnest(?::varchar[])) or input_hash in (select unnest(?::varchar[]))",
                [coffee_ids, hashes],
            ).fetchall()
        finally:
            con.close()
        return [
            dict(zip(("coffee_id", "input_hash", "roaster_id", "platform"), r, strict=True)) for r in rows
        ]

    def of_roasters(self, build_id: str, roaster_ids: list[str]) -> list[dict]:
        """The build's public coffees (in dim_coffee) of these stores: what Not a roaster takes out of the catalog."""
        if not roaster_ids:
            return []
        self.ensure(build_id)
        con = duckdb.connect(str(self.path(build_id)), read_only=True)
        try:
            rows = con.execute(
                "select coffee_id, input_hash, roaster_id, title from products "
                "where in_dim_coffee and roaster_id in (select unnest(?::varchar[])) order by coffee_id",
                [roaster_ids],
            ).fetchall()
        finally:
            con.close()
        return [dict(zip(("coffee_id", "input_hash", "roaster_id", "title"), r, strict=True)) for r in rows]

    def search(self, build_id: str, f: Filters) -> dict:
        try:
            meta = self.ensure(build_id)
            con = duckdb.connect(str(self.path(build_id)), read_only=True)
        except duckdb.IOException:  # pruned by another request between ensure and connect: make it again
            meta = self.ensure(build_id)
            con = duckdb.connect(str(self.path(build_id)), read_only=True)
        try:
            where, params = f.where()
            total = _one(con, "select count(*) from products")
            matching = _one(con, f"select count(*) from products {where}", params)
            rows = con.execute(
                f"""select coffee_id, roaster_id, product_id, title, coffee_name, product_type, url, is_listed,
                           is_coffee, coffee, classified_by, verdict_label, reason, origin_country, process, stock,
                           min_price_usd, price_per_100g, listed, last_seen, input_hash, platform
                    from products {where}
                    order by roaster_id, title, coffee_id limit {PAGE} offset {(f.page - 1) * PAGE}""",
                params,
            )
            cols = [d[0] for d in rows.description]
            items = [dict(zip(cols, r, strict=True)) for r in rows.fetchall()]
            for it in items:
                it["url"] = safe_url(it["url"])
            facets = {}
            for name in FACETS:
                w, p = f.where(skip=name)
                col = COLUMN[name]
                facets[name] = con.execute(
                    f"""select coalesce({col}::varchar, '(none)') as value, count(*) as n from products {w}
                        group by 1 order by n desc, value limit {FACET_VALUES}""",
                    p,
                ).fetchall()
            return {
                "meta": meta,
                "total": total,
                "matching": matching,
                "rows": items,
                "facets": facets,
                "page": f.page,
                "pages": max(1, -(-matching // PAGE)),
            }
        finally:
            con.close()


# --- keyword filters: what a filter matches in a build ------------------------------------------------------------

MATCH_COLS = ("coffee_id", "roaster_id", "title", "product_type", "tags", "url", "input_hash", "reason",
              "verdict_label", "in_dim_coffee", "is_coffee", "unfiltered_is_coffee", "is_listed")  # fmt: skip
SQL_FIELD = {"title": "kf_title", "product_type": "kf_type", "tags": "kf_tags"}


class PrefilterReader:
    """The keyword filters against one build's products (its catalog file): what each newly excludes, and what a
    change to the set newly excludes or gives back. The same classify code as the build decides: SQL only narrows the
    candidates to a superset (the phrase's first word as a substring of the same normalized text the filter reads),
    and classify.KeywordFilter.matches decides the whole-word match."""

    def __init__(self, catalog: Catalog, build_id: str):
        self.catalog, self.build_id = catalog, build_id

    def has_tags(self) -> bool:
        return self.catalog.ensure(self.build_id).get("tags") == "yes"

    def candidates(self, filters: list[KeywordFilter]) -> list[dict]:
        """The build's products any of `filters` might match (each once)."""
        if not filters:
            return []
        self.catalog.ensure(self.build_id)
        where = " or ".join(f"{SQL_FIELD[f.field]} like ? escape '\\'" for f in filters)
        like = ["%" + f.phrase.split(" ")[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                for f in filters]  # fmt: skip
        con = duckdb.connect(str(self.catalog.path(self.build_id)), read_only=True)
        try:
            rows = con.execute(f"select {', '.join(MATCH_COLS)} from products where {where} "
                               "order by roaster_id, title, coffee_id", like).fetchall()  # fmt: skip
        finally:
            con.close()
        return [dict(zip(MATCH_COLS, r, strict=True)) for r in rows]

    @staticmethod
    def _item(r: dict):
        return products.item(r["title"], r["product_type"], list(r["tags"] or []), None, None, None)

    def counts(self, filters: list[KeywordFilter]) -> tuple[dict[KeywordFilter, int], int]:
        """How many products each filter newly excludes in the build, and how many they exclude together: a listed
        product no rule of the code excludes and no owner's decision settles, counted for the first filter that
        matches it (in the filters' order, as the build names it)."""
        ordered = tuple(sorted(filters))
        out = dict.fromkeys(filters, 0)
        for r in self.candidates(list(ordered)):
            if (r["reason"] or "").startswith("decision:") or r["is_listed"] is False:
                continue
            it = self._item(r)
            if classify.exclusion(it) is not None:
                continue
            if hit := classify.keyword_match(it, ordered):
                out[hit] += 1
        return out, sum(out.values())

    def changed(self, old: tuple[KeywordFilter, ...], new: tuple[KeywordFilter, ...]) -> list[dict]:
        """The products whose answer the change from `old` to `new` turns: excluded by the new set and not the old
        (`excluded`), or the reverse (`restored`). Products the code's rules exclude, and those an owner's decision
        settles, are left out: no filter changes them. Each row says which way, by which filter, and whether it is a
        public coffee now."""
        diff = sorted(set(old) ^ set(new))
        out = []
        for r in self.candidates(diff):
            if (r["reason"] or "").startswith("decision:") or r["is_listed"] is False:
                continue
            it = self._item(r)
            if classify.exclusion(it) is not None:  # the code's own rules: no filter changes it
                continue
            was, now = classify.keyword_match(it, old), classify.keyword_match(it, new)
            if (was is None) == (now is None):
                continue
            out.append(
                {k: r[k] for k in ("coffee_id", "roaster_id", "title", "url", "input_hash", "verdict_label",
                                   "unfiltered_is_coffee")}
                | {"way": "excluded" if now else "restored", "filter": (now or was).name,  # type: ignore[union-attr]
                   "public": bool(r["in_dim_coffee"]), "url": safe_url(r["url"])}
            )  # fmt: skip
        return out
