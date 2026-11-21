"""Null WooCommerce size prices in the stored snapshots: their cause, and what the 2026-11-16 crawl did to them.

    uv run python eval/crawl/woo_null_prices.py <a read-only local copy of the lake> [--per-roaster out.csv]

Reads `snapshots` only, with DuckDB; sends no request and writes nothing to the lake. Prints totals and the
sample's Wilson interval; the per-roaster table goes to a file outside git when asked for (eval/crawl/README.md,
"2026-11-20").
"""

from __future__ import annotations

import math
import sys

import duckdb

SALT = "coffeedata-woo-prices-2026-11-20"
OLD_CAP = 150  # the adapter's MAX_VARIATION_LOOKUPS until #99
FIRST_UNCAPPED_RUN = "2026-11-16"
PER_ROASTER = 2  # sampled sizes per roaster


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p, d = k / n, 1 + z * z / n
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return ((p + z * z / (2 * n)) / d - half, (p + z * z / (2 * n)) / d + half)


def load(con: duckdb.DuckDBPyConnection, lake: str) -> None:
    """s: one row per stored size. `seq` is the size's place among its fetch's sizes that needed their own lookup
    (a product whose sizes differ in price, or have a null one), in the stored order, which is the listing's: the old
    adapter asked for the first OLD_CAP of them and left the rest null."""
    con.execute(
        f"""create table s as
        with p as (select run_date, batch_id, roaster_id, product_id, variants, file_row_number as rn
                   from read_parquet('{lake}/snapshots/*/*.parquet', union_by_name = true, hive_partitioning = true,
                                     file_row_number = true)
                   where platform = 'woocommerce'),
        x as (select p.* exclude (variants), unnest(variants, recursive := true),
                     generate_subscripts(variants, 1) as pos from p),
        y as (select *, variant_id <> product_id and (count(distinct price_usd) over w > 1
                         or count(*) filter (where price_usd is null) over w > 0) as needs
              from x window w as (partition by run_date, batch_id, roaster_id, product_id))
        select *, case when needs then sum(needs::int) over (
            partition by run_date, batch_id, roaster_id order by rn, pos) end as seq from y"""
    )
    last = "select roaster_id, max(run_date || '|' || batch_id) as k from s where run_date {} group by all"
    for name, cond in (("b", f"< '{FIRST_UNCAPPED_RUN}'"), ("a", f"= '{FIRST_UNCAPPED_RUN}'")):
        con.execute(
            f"""create table {name} as select s.* from s join ({last.format(cond)}) l
                on s.roaster_id = l.roaster_id and s.run_date || '|' || s.batch_id = l.k"""
        )
    con.execute(
        f"""create table j as
        select b.*, a.price_usd as after_price,
            case when b.roaster_id not in (select roaster_id from a) then 'store not read again'
                 when a.variant_id is null then 'no longer listed'
                 when a.price_usd is null then 'still null' else 'priced' end as outcome,
            sha256('{SALT}:' || b.roaster_id || '/' || b.product_id || '/' || b.variant_id) as h
        from b left join a using (roaster_id, product_id, variant_id)"""
    )


def main(argv: list[str]) -> None:
    con = duckdb.connect()
    load(con, argv[0].rstrip("/"))
    print("Null size prices by run and cause:")
    con.sql(
        f"""select run_date,
            case when variant_id = product_id then 'a product with no sizes'
                 when seq > {OLD_CAP} then 'past the {OLD_CAP}-lookup cap'
                 else 'a failed lookup stored as null' end as cause,
            count(*) as sizes, count(distinct roaster_id || '/' || product_id) as products,
            count(distinct roaster_id) as roasters
        from s where price_usd is null group by all order by 1, 2"""
    ).show()
    print("Every stored size, by run (a priced size past the cap would contradict the cause):")
    con.sql(
        f"""select run_date, count(distinct roaster_id) as roasters, count(*) as sizes,
            count(*) filter (where price_usd is null) as null_sizes,
            count(*) filter (where seq > {OLD_CAP} and price_usd is not null) as priced_past_the_cap
        from s group by all order by 1"""
    ).show()
    print(f"Sizes null in each roaster's last snapshot before {FIRST_UNCAPPED_RUN}, and that run's answer:")
    con.sql(
        """select outcome, count(*) as sizes, count(distinct roaster_id || '/' || product_id) as products,
            count(distinct roaster_id) as roasters from j where price_usd is null group by all order by 2 desc"""
    ).show()
    print("Agreement of the two reads where both have a price (the one-by-one lookups, then the bulk query):")
    con.sql(
        """select count(*) as sizes, count(*) filter (where price_usd = after_price) as same_price,
            count(distinct roaster_id) filter (where price_usd <> after_price) as roasters_that_differ
        from j where price_usd is not null and after_price is not null
          and roaster_id in (select roaster_id from j where price_usd is null)"""
    ).show()
    sample = con.execute(
        f"""select count(*), count(*) filter (where outcome = 'priced'), count(distinct roaster_id) from (
            select *, row_number() over (partition by roaster_id order by h) as k from j where price_usd is null)
        where k <= {PER_ROASTER}"""
    ).fetchone()
    assert sample is not None
    n, k, roasters = sample
    lo, hi = wilson(k, n)
    print(
        f"Sample ({PER_ROASTER} lowest hashes per roaster, salt {SALT}): {k} of {n} priced at {roasters} roasters,"
    )
    print(f"Wilson 95% {lo:.3f} to {hi:.3f}.")
    if "--per-roaster" in argv:
        out = argv[argv.index("--per-roaster") + 1]
        con.execute(
            f"""copy (select roaster_id, any_value(run_date) as last_run_before,
                    count(distinct product_id) as products, count(*) as sizes,
                    count(*) filter (where price_usd is null) as null_sizes,
                    count(distinct product_id) filter (where price_usd is null) as products_with_a_null_size,
                    count(*) filter (where price_usd is null and outcome = 'priced') as now_priced,
                    count(*) filter (where price_usd is null and outcome = 'no longer listed') as no_longer_listed,
                    count(*) filter (where price_usd is null and outcome = 'still null') as still_null,
                    count(*) filter (where price_usd is null and outcome = 'store not read again') as not_read_again
                from j group by all having null_sizes > 0 order by null_sizes desc) to '{out}' (header)"""
        )


if __name__ == "__main__":
    main(sys.argv[1:])
