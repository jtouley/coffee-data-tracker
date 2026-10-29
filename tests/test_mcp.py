import json

import pytest

from coffeedata import mcp_server, shots


def test_check_sql():
    assert mcp_server.check_sql("select 1;") == "select 1"
    assert mcp_server.check_sql("WITH x AS (SELECT 1) SELECT * FROM x")
    for bad in [
        "drop table dim_coffee",
        "select 1; drop table x",
        "insert into t values (1)",
        "copy x to 'y'",
    ]:
        with pytest.raises(ValueError):
            mcp_server.check_sql(bad)


@pytest.mark.anyio
async def test_tools_over_mcp_protocol(lake, tmp_path, monkeypatch):
    """Drive the real server through an in-process MCP client."""
    import duckdb
    from mcp import Client

    db = tmp_path / "c.duckdb"
    con = duckdb.connect(str(db))
    con.execute("""create table dim_coffee as select 'onyxcoffeelab.com:1' as coffee_id, 'Onyx Coffee Lab' as roaster_name,
                   'Geometry' as title, 'u' as url, 'Ethiopia' as origin_country, 'washed' as process,
                   ['peach'] as tasting_notes, true as in_stock, true as is_listed, 3.5 as price_per_100g,
                   'onyxcoffeelab.com' as roaster_id""")
    lake.write_mart("dim_coffee", con, "select * from dim_coffee")
    con.close()
    monkeypatch.setattr(mcp_server, "DB", db)
    monkeypatch.setattr(
        shots, "_index", lambda lake=None: {"onyxcoffeelab.com:1": "Onyx Coffee Lab Geometry"}
    )

    async with Client(mcp_server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"log_shot", "search_coffees", "shot_history", "suggest_next", "run_sql"} <= names

        r = await client.call_tool(
            "log_shot",
            {
                "coffee": "onyx geometry",
                "dose_g": 18,
                "yield_g": 36,
                "time_s": 22,
                "grinder_setting": 15,
                "rating": 3,
            },
        )
        assert "onyxcoffeelab.com:1" in r.content[0].text and "finer" in r.content[0].text

        r = await client.call_tool(
            "log_shot",
            {
                "coffee": "onyx geometry",
                "dose_g": 18,
                "yield_g": 36,
                "time_s": 28,
                "grinder_setting": 15,
                "rating": 3,
                "grinder_name": "EK43",
            },
        )
        assert r.is_error

        r = await client.call_tool("search_coffees", {"query": "geometry", "process": "washed"})
        assert "Geometry" in r.content[0].text

        r = await client.call_tool("run_sql", {"sql": "select coffee_id, title from dim_coffee"})
        assert json.loads(r.content[0].text)["rows"] == [["onyxcoffeelab.com:1", "Geometry"]]

        r = await client.call_tool("run_sql", {"sql": "drop table dim_coffee"})
        assert r.is_error

        # Review SF-2: no file access, even through a single SELECT.
        secret = tmp_path / "secret.txt"
        secret.write_text("topsecret-token")
        for sql in (
            f"select content from read_text('{secret}')",
            f"select * from read_csv('{secret}')",
            "select current_setting('enable_external_access')",
        ):
            r = await client.call_tool("run_sql", {"sql": sql})
            assert r.is_error or "topsecret" not in r.content[0].text
        r = await client.call_tool("run_sql", {"sql": f"select content from read_text('{secret}')"})
        assert r.is_error


@pytest.mark.parametrize(
    "with_status", [True, False], ids=["current export", "export from before catalog_status"]
)
def test_search_labels_the_back_catalog(lake, with_status):
    import duckdb

    status = ", 'archived' as catalog_status" if with_status else ""
    con = duckdb.connect()
    con.execute(f"""create table dim_coffee as select 'onyxcoffeelab.com:9' as coffee_id, 'Onyx Coffee Lab' as roaster_name,
                    'Kenya Kamavindi' as title, 'u' as url, 'Kenya' as origin_country, 'washed' as process,
                    ['plum'] as tasting_notes, false as in_stock, true as is_listed, null::double as price_per_100g,
                    'onyxcoffeelab.com' as roaster_id{status}""")  # fmt: skip
    lake.write_mart("dim_coffee", con, "select * from dim_coffee")
    search = getattr(mcp_server.search_coffees, "fn", mcp_server.search_coffees)
    [row] = search(in_stock_only=False)
    assert row["catalog_status"] == ("archived" if with_status else None)
    assert search() == []  # in stock only, by default


def test_search_reads_old_and_new_exports_together(lake, monkeypatch):
    # Review #4: the schema check and the query read the files the same way (union_by_name), so a mix of exports
    # from before and after is_wholesale filters the new ones' wholesale rows and keeps every old row.
    import duckdb

    from coffeedata.lake import Lake

    row = """select 'onyxcoffeelab.com:{n}' as coffee_id, 'Onyx Coffee Lab' as roaster_name, '{title}' as title,
             'u' as url, 'Ethiopia' as origin_country, 'washed' as process, ['plum'] as tasting_notes,
             true as in_stock, true as is_listed, 3.0 as price_per_100g, 'onyxcoffeelab.com' as roaster_id"""
    con = duckdb.connect()
    old, new = lake.root / "marts" / "old.parquet", lake.root / "marts" / "new.parquet"
    old.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"copy ({row.format(n=1, title='Kenya Kiambu')}) to '{old}'")
    con.execute(f"""copy ({row.format(n=2, title="Ethiopia Guji")}, false as is_wholesale
                    union all {row.format(n=3, title="Wholesale - 5LB Ethiopia Guji")}, true) to '{new}'""")
    monkeypatch.setattr(Lake, "mart_sources", lambda self, name: [str(old), str(new)])
    search = getattr(mcp_server.search_coffees, "fn", mcp_server.search_coffees)
    assert sorted(r["title"] for r in search()) == ["Ethiopia Guji", "Kenya Kiambu"]
    assert len(search(include_wholesale=True)) == 3
