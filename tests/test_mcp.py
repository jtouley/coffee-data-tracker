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
async def test_tools_over_mcp_protocol(tmp_path, monkeypatch):
    """Drive the real server through an in-memory MCP client session."""
    import duckdb
    from mcp import Client

    db = tmp_path / "c.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table dim_coffee as select 'onyxcoffeelab:geometry' as coffee_id, 'Geometry' as title")
    con.close()
    monkeypatch.setattr(mcp_server, "DB", db)
    monkeypatch.setattr(shots, "SHOTS", tmp_path / "shots.jsonl")
    monkeypatch.setattr(shots, "_index", lambda: {"onyxcoffeelab:geometry": "Onyx Coffee Lab Geometry"})

    async with Client(mcp_server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"log_shot", "search_coffees", "shot_history", "suggest_next", "run_sql"} <= names

        r = await client.call_tool("log_shot", {"coffee": "onyx geometry", "dose_g": 18, "yield_g": 36,
                                                "time_s": 22, "grinder_setting": 15, "rating": 3})
        assert "onyxcoffeelab:geometry" in r.content[0].text and "finer" in r.content[0].text

        r = await client.call_tool("run_sql", {"sql": "select coffee_id, title from dim_coffee"})
        payload = json.loads(r.content[0].text)
        assert payload["rows"] == [["onyxcoffeelab:geometry", "Geometry"]]

        r = await client.call_tool("run_sql", {"sql": "drop table dim_coffee"})
        assert r.is_error
