import types
from datetime import date

import pytest

from coffeedata.lake import LAKE_TABLES, SCHEMA_FILES, Lake


def test_empty_lake_is_queryable(lake):
    con = lake.connect()
    for table in lake.tables:
        assert con.execute(f"select count(*) from {lake.scan(table)}").fetchone()[0] == 0


def test_files_are_immutable(lake):
    row = {"run_date": date(2026, 10, 26), "batch_id": "b1", "roaster_id": "x.com", "status": "ok"}
    lake.write_partition("fetches", "2026-10-26", "b1", [row])
    with pytest.raises(FileExistsError):
        lake.write_partition("fetches", "2026-10-26", "b1", [row])
    assert (
        lake.write_partition("fetches", "2026-10-26", "b2", []) is None
    )  # nothing to write, nothing written


def test_remote_lake_reads_staging_and_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    lake = Lake("hf://datasets/someone/coffee")
    assert lake.remote and lake.root == tmp_path / "staging"
    assert lake.sources("shots") == [
        str(tmp_path / "staging/shots/*.parquet"),
        "hf://datasets/someone/coffee/shots/*.parquet",
    ]


def test_publish_never_replaces_lake_files_but_replaces_marts(tmp_path, monkeypatch):
    remote = {
        "fetches/run_date=2026-10-26/batch=b1.parquet",
        "marts/dim_coffee.parquet",
        *(SCHEMA_FILES[t] for t in LAKE_TABLES),
    }
    commits = []

    class FakeApi:
        def __init__(self, token=None):
            pass

        def get_paths_info(self, repo_id, paths, repo_type):
            return [types.SimpleNamespace(path=p) for p in paths if p in remote]

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            commits.append(sorted(op.path_in_repo for op in operations))

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    lake = Lake("hf://datasets/someone/coffee")
    assert lake.publish("nothing staged") == 0 and commits == []  # schema files alone don't trigger a commit
    row = {"run_date": date(2026, 11, 2), "batch_id": "b2", "roaster_id": "x.com", "status": "ok"}
    lake.write_partition("fetches", "2026-11-02", "b2", [row])
    lake.write_partition("fetches", "2026-10-26", "b1", [row])  # already uploaded: skipped, not replaced
    (lake.root / "marts").mkdir()
    import duckdb

    lake.write_mart("dim_coffee", duckdb.connect(), "select 1 as x")
    assert lake.publish("test") == 2
    assert commits == [["fetches/run_date=2026-11-02/batch=b2.parquet", "marts/dim_coffee.parquet"]]
    assert lake.staged_files() == sorted(lake.root / SCHEMA_FILES[t] for t in LAKE_TABLES)  # staging cleared


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

        def get_paths_info(self, repo_id, paths, repo_type):
            return []

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            calls.append(("commit", len(operations)))

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setenv("COFFEEDATA_LAKE", "hf://datasets/someone/coffee")
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    cli.main(["init"])
    assert calls == [("create", "someone/coffee", True), ("card", "README.md"), ("commit", len(LAKE_TABLES))]


def test_bad_run_date_is_a_usage_error(capsys):
    from coffeedata import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["ingest", "--run-date", "2026-13-40"])
    assert e.value.code == 2 and "not a YYYY-MM-DD date" in capsys.readouterr().err


def test_publish_retries_and_tolerates_a_commit_that_landed(tmp_path, monkeypatch):
    """A transient error is retried; a commit whose response was lost isn't re-sent or refused."""
    remote: set[str] = set()
    commits = []

    class FlakyApi:
        def __init__(self, token=None):
            pass

        def get_paths_info(self, repo_id, paths, repo_type):
            return [types.SimpleNamespace(path=p) for p in paths if p in remote]

        def create_commit(self, repo_id, repo_type, operations, commit_message):
            paths = [op.path_in_repo for op in operations]
            commits.append(paths)
            if len(commits) == 1:
                raise ConnectionError("reset by peer")  # nothing landed
            remote.update(paths)
            if len(commits) == 2:
                raise TimeoutError("response lost")  # landed, but the client never heard

    import huggingface_hub

    from coffeedata import lake as lake_mod

    monkeypatch.setattr(huggingface_hub, "HfApi", FlakyApi)
    monkeypatch.setattr(lake_mod.time, "sleep", lambda s: None)
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    lake = Lake("hf://datasets/someone/coffee")
    row = {"run_date": date(2026, 11, 2), "batch_id": "b3", "roaster_id": "x.com", "status": "ok"}
    lake.write_partition("fetches", "2026-11-02", "b3", [row])
    assert lake.publish("test") == 0  # the third attempt finds the file already there
    assert len(commits) == 2 and "fetches/run_date=2026-11-02/batch=b3.parquet" in remote
    assert lake.staged_files() == sorted(lake.root / SCHEMA_FILES[t] for t in LAKE_TABLES)


def test_publish_keeps_staged_files_when_every_attempt_fails(tmp_path, monkeypatch):
    class DownApi:
        def __init__(self, token=None):
            pass

        def get_paths_info(self, repo_id, paths, repo_type):
            raise ConnectionError("down")

    import huggingface_hub

    from coffeedata import lake as lake_mod

    monkeypatch.setattr(huggingface_hub, "HfApi", DownApi)
    monkeypatch.setattr(lake_mod.time, "sleep", lambda s: None)
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    lake = Lake("hf://datasets/someone/coffee")
    row = {"run_date": date(2026, 11, 2), "batch_id": "b4", "roaster_id": "x.com", "status": "ok"}
    lake.write_partition("fetches", "2026-11-02", "b4", [row])
    with pytest.raises(ConnectionError):
        lake.publish("test")
    assert lake.root / "fetches/run_date=2026-11-02/batch=b4.parquet" in lake.staged_files()


def test_photos_live_in_their_own_dataset_in_hf_image_layout(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime

    import pyarrow.parquet as pq

    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_PHOTOS", str(tmp_path / "photos"))
    main, photos = Lake(), Lake.photos()
    assert not (main.root / "photos").exists()  # no photo table in the main lake, ever
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
    p = photos.write(
        "photos",
        "photos/p1.parquet",
        [
            {
                "photo_id": "p1",
                "user_id": "owner",
                "taken_at": datetime(2026, 10, 27, tzinfo=UTC),
                "image": {"bytes": jpeg, "path": "p1.jpg"},
                "media_type": "image/jpeg",
                "bytes": len(jpeg),
            }
        ],
    )
    assert p is not None
    t = pq.read_table(p)
    assert t.column("image")[0].as_py() == {"bytes": jpeg, "path": "p1.jpg"}
    features = json.loads(t.schema.metadata[b"huggingface"])["info"]["features"]
    assert features["image"] == {"_type": "Image"} and features["photo_id"]["dtype"] == "string"
