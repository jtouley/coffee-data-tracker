"""Review 2026-11-21, F5: the app's DuckDB reads pass file paths as parameters, never spliced into SQL, and
photos.load checks the photo id itself (SAFE_ID), whatever its caller checked."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest

from app import photos
from coffeedata.lake import Lake

APP = Path(__file__).parent.parent / "app"


def test_no_app_sql_splices_a_path_into_read_parquet():
    spliced = re.compile(r"read_parquet\(\s*\[?\s*['\"]?\{")
    found = [
        f"{p.name}:{n}" for p in APP.glob("*.py") for n, line in enumerate(p.read_text().splitlines(), 1)
        if spliced.search(line)
    ]  # fmt: skip
    assert found == []


@pytest.mark.parametrize("bad", ["x'); select 1; --", "../users/u1", "a/b", "", "a" * 65, "p 1"])
def test_photos_load_refuses_an_id_that_isnt_plain(tmp_path, monkeypatch, bad):
    lake = Lake(str(tmp_path / "photos"))
    ran: list[str] = []
    real = lake.connect

    def spy(*a, **k):
        con = real(*a, **k)
        ran.append("connect")
        return con

    monkeypatch.setattr(lake, "connect", spy)
    assert photos.load(lake, bad) is None
    assert ran == []  # turned away before any SQL


def test_photos_load_reads_a_file_whose_folder_has_a_quote(tmp_path):
    """A path with a quote in it is just a path now: before, it broke the statement (or became part of it)."""
    root = tmp_path / "it's here" / "photos"
    lake = Lake(str(root))
    folder = Path(lake.sources("photos")[0]).parent
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "copy (select 'u1' as user_id, {'bytes': 'jpeg'::blob} as image) to ?", [str(folder / "p1.parquet")]
    )
    assert photos.load(lake, "p1") == ("u1", b"jpeg")
