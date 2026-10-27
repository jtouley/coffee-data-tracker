import json

import pytest

from coffeedata import mcp_server, shots


def test_check_sql():
    assert mcp_server.check_sql("select 1;") == "select 1"
    assert mcp_server.check_sql("WITH x AS (SELECT 1) SELECT * FROM x")
    for bad in ["drop table dim_coffee", "select 1; drop table x", "insert into t values (1)", "copy x to 'y'"]:
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
    monkeypatch.setattr(shots, "_index", lambda lake=None: {"onyxcoffeelab.com:1": "Onyx Coffee Lab Geometry"})

    async with Client(mcp_server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"log_shot", "search_coffees", "shot_history", "suggest_next", "run_sql"} <= names

        r = await client.call_tool("log_shot", {"coffee": "onyx geometry", "dose_g": 18, "yield_g": 36,
                                                "time_s": 22, "grinder_setting": 15, "rating": 3})
        assert "onyxcoffeelab.com:1" in r.content[0].text and "finer" in r.content[0].text

        r = await client.call_tool("search_coffees", {"query": "geometry", "process": "washed"})
        assert "Geometry" in r.content[0].text

        r = await client.call_tool("run_sql", {"sql": "select coffee_id, title from dim_coffee"})
        assert json.loads(r.content[0].text)["rows"] == [["onyxcoffeelab.com:1", "Geometry"]]

        r = await client.call_tool("run_sql", {"sql": "drop table dim_coffee"})
        assert r.is_error

        # Review SF-2: no file access, even through a single SELECT.
        secret = tmp_path / "secret.txt"
        secret.write_text("topsecret-token")
        for sql in (f"select content from read_text('{secret}')", f"select * from read_csv('{secret}')",
                    "select current_setting('enable_external_access')"):
            r = await client.call_tool("run_sql", {"sql": sql})
            assert r.is_error or "topsecret" not in r.content[0].text
        r = await client.call_tool("run_sql", {"sql": f"select content from read_text('{secret}')"})
        assert r.is_error
