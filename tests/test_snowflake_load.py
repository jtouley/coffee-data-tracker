import json

import pytest

from coffeedata import snowflake_load
from coffeedata.public import PUBLIC_TABLES


class FakeCursor:
    def __init__(self, counts):
        self.sql, self.counts, self._last = [], counts, None

    def execute(self, sql):
        self.sql.append(sql)
        self._last = sql
        return self

    def fetchone(self):
        table = self._last.rsplit(".", 1)[-1]
        return (self.counts[table],)

    def close(self):
        pass


class FakeConn:
    def __init__(self, counts):
        self.cur = FakeCursor(counts)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def export_dir(tmp_path, counts, exported_at="2026-10-27T02:01:48+00:00"):
    for t in PUBLIC_TABLES:
        (tmp_path / f"{t}.parquet").write_bytes(b"PAR1")
    (tmp_path / "manifest.json").write_text(json.dumps({"as_of_run": "2026-10-26", "exported_at": exported_at,
                                                        "tables": counts}))
    return tmp_path


def test_reexporting_a_week_loads_the_new_files(tmp_path):
    """A second export of the same week goes to its own folder, so COPY can't pick up the first one's files."""
    counts = {t: 1 for t in PUBLIC_TABLES}
    paths = []
    for when in ("2026-10-27T02:01:48+00:00", "2026-10-27T09:30:00+00:00"):
        con = FakeConn(counts)
        snowflake_load.load(export_dir(tmp_path, counts, when), con)
        copy = next(s for s in con.cur.sql if s.startswith("copy into MARKET.public_coffee"))
        paths.append(copy.split(" from ")[1].split(" ")[0])
    assert paths == ["@MARKET.PUBLIC_EXPORT/run=2026-10-26/exported=20261027T020148Z/public_coffee.parquet",
                     "@MARKET.PUBLIC_EXPORT/run=2026-10-26/exported=20261027T093000Z/public_coffee.parquet"]


def test_load_stages_per_run_and_reloads_each_table(tmp_path):
    counts = {t: i + 1 for i, t in enumerate(PUBLIC_TABLES)}
    con = FakeConn(counts)
    out = snowflake_load.load(export_dir(tmp_path, counts), con)
    assert out == {"run": "2026-10-26", "tables": counts}
    sql = con.cur.sql
    puts = [s for s in sql if s.startswith("put ")]
    assert len(puts) == 4 and all("@MARKET.PUBLIC_EXPORT/run=2026-10-26/exported=" in p and "overwrite = false" in p for p in puts)
    for t in PUBLIC_TABLES:  # truncate + copy inside one transaction per table
        i = sql.index(f"truncate table MARKET.{t}")
        assert sql[i - 1] == "begin" and sql[i + 1].startswith(f"copy into MARKET.{t}") and sql[i + 2] == "commit"


def test_row_count_mismatch_fails(tmp_path):
    counts = {t: 5 for t in PUBLIC_TABLES}
    con = FakeConn(counts | {"public_coffee": 4})
    with pytest.raises(RuntimeError, match="row counts differ"):
        snowflake_load.load(export_dir(tmp_path, counts), con)


def test_private_key_pem_to_der():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    import base64

    expected = snowflake_load.private_key_der(pem)
    assert serialization.load_der_private_key(expected, password=None).key_size == 2048
    one_line = base64.b64encode(pem.encode()).decode()  # base64 -w0 coffee_rsa.p8
    escaped = pem.replace("\n", "\\n")                 # pasted with literal \n
    for variant in (one_line, escaped, "  " + pem + "\n"):
        assert snowflake_load.private_key_der(variant) == expected


def test_missing_credentials_is_a_clear_error(monkeypatch):
    for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(SystemExit, match="SNOWFLAKE_ACCOUNT"):
        snowflake_load.connect()
