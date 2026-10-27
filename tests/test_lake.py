from datetime import date

import pytest

from coffeedata.lake import SCHEMA_FILES, Lake


def test_empty_lake_is_queryable(lake):
    con = lake.connect()
    for table in SCHEMA_FILES:
        assert con.execute(f"select count(*) from {lake.scan(table)}").fetchone()[0] == 0


def test_files_are_immutable(lake):
    row = {"run_date": date(2026, 10, 26), "batch_id": "b1", "roaster_id": "x.com", "status": "ok"}
    lake.write_partition("fetches", "2026-10-26", "b1", [row])
    with pytest.raises(FileExistsError):
        lake.write_partition("fetches", "2026-10-26", "b1", [row])
    assert lake.write_partition("fetches", "2026-10-26", "b2", []) is None  # nothing to write, nothing written


def test_remote_lake_reads_staging_and_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    lake = Lake("hf://datasets/someone/coffee")
    assert lake.remote and lake.root == tmp_path / "staging"
    assert lake.sources("shots") == [str(tmp_path / "staging/shots/*.parquet"),
                                     "hf://datasets/someone/coffee/shots/*.parquet"]


def test_publish_refuses_to_overwrite_and_replaces_marts(tmp_path, monkeypatch):
    calls = {}

    class FakeApi:
        def __init__(self, token=None):
            pass

        def list_repo_files(self, repo_id, repo_type):
            return ["fetches/run_date=2026-10-26/batch=b1.parquet", "marts/dim_coffee.parquet",
                    *SCHEMA_FILES.values()]

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            calls["paths"] = sorted(op.path_in_repo for op in operations)

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    lake = Lake("hf://datasets/someone/coffee")
    row = {"run_date": date(2026, 11, 2), "batch_id": "b2", "roaster_id": "x.com", "status": "ok"}
    lake.write_partition("fetches", "2026-11-02", "b2", [row])
    (lake.root / "marts").mkdir()
    import duckdb
    lake.write_mart("dim_coffee", duckdb.connect(), "select 1 as x")
    assert lake.publish("test") == 2
    assert calls["paths"] == ["fetches/run_date=2026-11-02/batch=b2.parquet", "marts/dim_coffee.parquet"]
    assert lake.staged_files() == sorted(lake.root / p for p in SCHEMA_FILES.values())  # staging cleared

    lake.write_partition("fetches", "2026-10-26", "b1", [row])  # already in the remote dataset
    with pytest.raises(FileExistsError):
        lake.publish("again")


def test_init_creates_private_dataset_with_card(tmp_path, monkeypatch):
    import huggingface_hub

    from coffeedata import cli

    calls = []

    class FakeApi:
        def __init__(self, token=None):
            pass

        def create_repo(self, repo_id, repo_type, private, exist_ok):
            calls.append(("create", repo_id, private))
            return f"https://huggingface.co/datasets/{repo_id}"

        def upload_file(self, path_or_fileobj, path_in_repo, repo_id, repo_type, commit_message):
            calls.append(("card", path_in_repo))

        def list_repo_files(self, repo_id, repo_type):
            return []

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            calls.append(("commit", len(operations)))

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setenv("COFFEEDATA_LAKE", "hf://datasets/someone/coffee")
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    cli.main(["init"])
    assert calls == [("create", "someone/coffee", True), ("card", "README.md"), ("commit", len(SCHEMA_FILES))]


def test_bad_run_date_is_a_usage_error(capsys):
    from coffeedata import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["ingest", "--run-date", "2026-13-40"])
    assert e.value.code == 2 and "not a YYYY-MM-DD date" in capsys.readouterr().err
