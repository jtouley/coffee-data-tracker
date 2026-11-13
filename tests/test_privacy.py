"""What the privacy policy (https://registry.coffee/privacy/) says the app does, checked against the app."""

import inspect
import json
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app import labels
from coffeedata import erase
from coffeedata.lake import LAKE_TABLES, PHOTO_TABLES, USER_TABLES, Lake

from .test_bags import LABEL, FakeClient
from .test_invites import PW, W, accounts, app, client, invite, join, owner  # noqa: F401 - fixtures

ROOT = Path(__file__).parent.parent


def every_value(lake: Lake | Path) -> str:
    """Everything stored in a local lake (or one folder of it), as text."""
    root = lake.root if isinstance(lake, Lake) else lake
    return " ".join(
        json.dumps(row, default=str) for f in root.rglob("*.parquet") for row in pq.read_table(f).to_pylist()
    )


# 1. EXIF is removed from bag photos before they're stored: tests/test_bags.py
#    (test_photo_is_downsized_and_loses_its_metadata, test_photo_upload_reads_matches_and_stores_the_photo).


def test_ip_addresses_are_kept_in_memory_only_never_stored_or_logged(  # noqa: F811
    app,  # noqa: F811
    accounts,  # noqa: F811
    lake,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("COFFEEDATA_PROXY_HOPS", "1")
    ip = "203.0.113.77"
    with caplog.at_level(logging.DEBUG):
        c = client(app)
        h = W | {"X-Forwarded-For": ip}
        c.post(
            "/api/signin",
            json={"agree": True, "setup_code": "SETUP-CODE-1234", "username": "jtouley", "password": PW},
            headers=h,
        )
        c.post("/api/signin", json={"username": "jtouley", "password": "wrong password!"}, headers=h)
        c.get("/", headers=h)
    assert ip not in every_value(accounts.lake) and ip not in every_value(lake)
    assert not any(ip in r.getMessage() or ip in json.dumps(getattr(r, "fields", {})) for r in caplog.records)


def test_passwords_are_stored_only_as_a_hash(owner, accounts):  # noqa: F811
    stored = every_value(accounts.lake)
    assert PW not in stored and "$argon2id$" in stored


def test_the_app_uses_exactly_the_cookies_the_policy_lists(app, owner):  # noqa: F811
    # The server sets the session cookie; the page script sets theme, hand and tz.
    names = set()
    c = client(app)
    for r in (
        c.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=W),
        c.get("/"),
        c.get("/setup"),
        c.post("/api/signout", json={}, headers=W),
    ):
        names |= {h.split("=", 1)[0] for h in r.headers.get_list("set-cookie")}
    js = (ROOT / "app/static/app.js").read_text()
    assert (
        js.count("document.cookie =") == 3
    )  # tz, the one in preference(), and Today's "Not now" (nudge-email)
    names |= set(re.findall(r"document\.cookie = `([\w-]+)=", js))
    names |= set(re.findall(r'preference\(\w+, "(\w+)"', js))
    assert names == {"session", "theme", "hand", "tz", "nudge-email"}
    policy = (ROOT / "src/coffeedata/templates/privacy.html").read_text()
    assert set(re.findall(r"<li><code>([\w-]+)</code>", policy)) == names


def test_anthropic_receives_only_the_photo_and_the_fixed_prompt():
    fake = FakeClient(LABEL)
    labels.read(b"\xff\xd8jpeg", labels.OPUS, fake)
    ((_, request),) = fake.calls
    assert set(request) <= {"model", "max_tokens", "output_config", "messages", "betas", "fallbacks"}
    (message,) = request["messages"]
    kinds = [part["type"] for part in message["content"]]
    assert kinds == ["image", "text"]
    assert message["content"][1]["text"] == labels.PROMPT.format(year=date.today().year)
    # The reader is never given who took the photo, and the request carries no metadata.
    assert set(inspect.signature(labels.read).parameters) == {"jpeg", "model_id", "client"}
    assert "metadata" not in request


# 6. Deleting a person's data on request.


@pytest.fixture
def three(tmp_path):
    main = Lake(str(tmp_path / "lake"))
    photos = Lake(str(tmp_path / "photos"), tables=PHOTO_TABLES)
    users = Lake(str(tmp_path / "users"), tables=USER_TABLES)
    t = datetime(2026, 10, 20, 7, tzinfo=UTC)
    later = datetime(2026, 10, 21, 7, tzinfo=UTC)
    for uid, name, role in (("owner", "jtouley", "owner"), ("u-maya", "maya", "member")):
        users.write("users", f"users/{uid}__20261020T070000.parquet",
                    [{"user_id": uid, "username": name, "role": role, "logged_at": t}])  # fmt: skip
        shot = {"shot_id": f"s-{uid}", "user_id": uid, "logged_at": t, "brewed_at": t, "dose_g": 18.0,
                "yield_g": 36.0, "time_s": 28.0, "grinder_setting": 11.0, "rating": 4}  # fmt: skip
        main.write("shots", f"shots/s-{uid}__20261020T070000.parquet", [shot])
        main.write(
            "bags",
            f"bags/b-{uid}__20261020T070000.parquet",
            [{"bag_id": f"b-{uid}", "user_id": uid, "logged_at": t}],
        )
        photos.write("photos", f"photos/p-{uid}.parquet", [{"photo_id": f"p-{uid}", "user_id": uid}])
        # The recommendation test's records (invented coffee): a rating with its undo, and an answer to a pick.
        for at, deleted in ((t, False), (later, True)):
            main.write("ratings", f"ratings/r-{uid}__{at:%Y%m%dT%H%M%S}.parquet",
                       [{"rating_id": f"r-{uid}", "user_id": uid, "coffee_id": "roaster.example:1", "rating": 4,
                         "context": "seed", "deleted": deleted, "logged_at": at}])  # fmt: skip
        main.write("rec_responses", f"rec_responses/{uid}__slate-1__h__20261020T070000.parquet",
                   [{"user_id": uid, "slate_id": "slate-1", "coffee_id": "roaster.example:1", "interest": "want",
                     "picked": True, "logged_at": t}])  # fmt: skip
    # Maya's account is turned off in the app before her data is deleted.
    users.write("users", "users/u-maya__20261021T070000.parquet",
                [{"user_id": "u-maya", "username": "maya", "role": "member", "disabled": True, "logged_at": later}])  # fmt: skip
    # Her invite: first unused, then used by her. Both versions must go, or it would work again (#24 review 1).
    users.write("invites", "invites/i1__20261020T070000.parquet",
                [{"invite_id": "i1", "created_by": "owner", "kind": "join", "logged_at": t,
                  "expires_at": datetime(2099, 1, 1, tzinfo=UTC)}])  # fmt: skip
    users.write("invites", "invites/i1__20261021T070000.parquet",
                [{"invite_id": "i1", "created_by": "owner", "kind": "join", "used_by": "u-maya", "logged_at": later,
                  "expires_at": datetime(2099, 1, 1, tzinfo=UTC)}])  # fmt: skip
    # The weekly shot export the app seeds from, built before the deletion (#24 review 3).
    con = main.connect()
    main.write_mart(
        "fct_shot",
        con,
        f"""select * from read_parquet('{main.root}/shots/*.parquet', union_by_name = true)
        cross join (select null::varchar shot_coffee_id, null::varchar shot_coffee_freetext, null::date shot_roast_date)""",
    )
    return main, photos, users


def test_delete_user_removes_every_version_of_their_records_and_nobody_elses(three):
    main, photos, users = three
    p = erase.plan("Maya", main, photos, users)
    assert p.user_id == "u-maya" and p.marts == ["fct_shot"]
    assert p.files == {
        main.url: ["bags/b-u-maya__20261020T070000.parquet", "ratings/r-u-maya__20261020T070000.parquet",
                   "ratings/r-u-maya__20261021T070000.parquet",
                   "rec_responses/u-maya__slate-1__h__20261020T070000.parquet",
                   "shots/s-u-maya__20261020T070000.parquet"],
        photos.url: ["photos/p-u-maya.parquet"],
        users.url: ["invites/i1__20261020T070000.parquet", "invites/i1__20261021T070000.parquet",
                    "users/u-maya__20261020T070000.parquet", "users/u-maya__20261021T070000.parquet"],
    }  # fmt: skip
    erase.apply(p, main, photos, users)
    left = every_value(main) + every_value(photos) + every_value(users)
    assert "u-maya" not in left and "s-owner" in left and "p-owner" in left and "r-owner" in left
    assert [r["user_id"] for r in main.latest("rec_responses", ("user_id", "slate_id", "coffee_id"))] == [
        "owner"
    ]
    assert not any(
        i["invite_id"] == "i1" for i in users.latest("invites", "invite_id")
    )  # the invite can't come back


def test_the_app_doesnt_load_a_deleted_persons_shots_back_from_the_export(three):
    from app.store import Store

    main, photos, users = three
    erase.apply(erase.plan("maya", main, photos, users), main, photos, users)
    assert set(Store(main, "owner").shots) == {"s-owner"}


def test_delete_user_needs_the_account_turned_off_first(three):
    # A turned-off account can't write (its sessions end, sign-in refuses it), so the running app can't recreate
    # the files being deleted (#24 review 4).
    main, photos, users = three
    users.write("users", "users/u-maya__20261022T070000.parquet",
                [{"user_id": "u-maya", "username": "maya", "role": "member", "disabled": False,
                  "logged_at": datetime(2026, 10, 22, 7, tzinfo=UTC)}])  # fmt: skip
    with pytest.raises(erase.EraseError, match="Turn maya's account off"):
        erase.plan("maya", main, photos, users)
    assert erase.plan("maya", main, photos, users, force=True).user_id == "u-maya"


def test_delete_user_refuses_the_owner_and_unknown_names(three):
    with pytest.raises(erase.EraseError, match="owner"):
        erase.plan("jtouley", *three)
    with pytest.raises(erase.EraseError, match="No account"):
        erase.plan("nobody", *three)


def test_delete_user_dry_run_deletes_nothing(three, monkeypatch, capsys):
    from coffeedata import cli

    main, photos, users = three
    monkeypatch.setattr(cli.Lake, "photos", classmethod(lambda cls: photos))
    monkeypatch.setattr(cli.Lake, "users", classmethod(lambda cls: users))
    monkeypatch.setenv("COFFEEDATA_LAKE", main.url)
    cli.main(["delete-user", "maya", "--dry-run"])
    out = capsys.readouterr().out
    assert "Dry run" in out and "marts/fct_shot.parquet" in out and "u-maya" in every_value(main)


# 7. The recommendation test's ratings and answers are private: no public output is built from them.

RECS_TABLES = ("ratings", "rec_responses")
_LAKE = re.compile(r"""\blake\(\s*['"](\w+)['"]\s*\)""")
_REF = re.compile(r"""\bref\(\s*['"](\w+)['"]\s*\)""")


def recs_descendants(models: dict[str, str], tables: tuple[str, ...] = RECS_TABLES) -> set[str]:
    """Every dbt model (name -> its SQL or Python) that reads one of these lake tables (ratings and rec_responses),
    directly or through other models, or sits in models/recs/ (its name given as recs/<name>)."""
    tainted = {n.split("/")[-1] for n, src in models.items()
               if n.startswith("recs/") or set(_LAKE.findall(src)) & set(tables)}  # fmt: skip
    refs = {n.split("/")[-1]: set(_REF.findall(src)) for n, src in models.items()}
    while grown := {n for n, r in refs.items() if r & tainted and n not in tainted}:
        tainted |= grown
    return tainted


def dbt_models() -> dict[str, str]:
    root = ROOT / "dbt" / "models"
    return {
        ("recs/" if "recs" in p.relative_to(root).parts else "") + p.stem: p.read_text()
        for p in [*root.rglob("*.sql"), *root.rglob("*.py")]
    }


def test_no_public_served_or_snowflake_table_is_built_from_ratings_or_answers():
    from coffeedata import public, quality

    outputs = set(public.PUBLIC_TABLES) | set(quality.RELEASE_TABLES) | set(quality.SERVED_MARTS)
    models = dbt_models()
    assert outputs <= set(models)  # the names are real models, so the check below can't pass by a typo
    assert not recs_descendants(models) & outputs
    # The check does catch one: a public table that reads them, directly or through a staging model.
    fake = {"stg_ratings": "select * from {{ lake('ratings') }}",
            "public_coffee": "select * from {{ ref('stg_ratings') }}", "recs/mart_rec_slate": "select 1",
            "public_roaster": "select * from {{ ref('mart_rec_slate') }}"}  # fmt: skip
    assert recs_descendants(fake) == {"stg_ratings", "public_coffee", "mart_rec_slate", "public_roaster"}


def test_the_public_exports_and_the_site_never_name_ratings_or_answers():
    # Snowflake loads public.PUBLIC_TABLES; the site and the public dataset are built from the release tables.
    for f in ("public.py", "snowflake_load.py", "site.py", "sitemap.py", "quality.py", "mcp_server.py"):
        text = (ROOT / "src/coffeedata" / f).read_text()
        assert not re.search(r"\b(ratings|rec_responses|mart_rec_\w+|rating_id|slate_id)\b", text), f
    for t in RECS_TABLES:  # they live in the private main lake, beside shots
        assert t in LAKE_TABLES


def test_every_exported_mart_built_from_personal_data_is_erased():
    # Review S5: a rec mart PR 3 exports to marts/ must be rewritten by delete-user too. (The facts-only guardrail's
    # new columns, slate_id, interest and picked, and a manifest-based descendant check come with those models.)
    from coffeedata import cli

    personal = ("shots", "bags", "equipment", "photos", *RECS_TABLES)
    built = recs_descendants(dbt_models(), personal)
    assert {"fct_shot", "mart_bag"} <= built  # the check sees the marts it already knows
    assert {m for m in cli.MARTS if m in built} <= set(erase.MARTS)
    assert set(erase.RECS_MARTS) <= set(erase.MARTS)


class Failing:
    def __init__(self, message):
        self.message = message

    def execute(self, *a):
        raise RuntimeError(self.message)


def test_a_mart_never_exported_is_skipped_and_listed(three, monkeypatch):
    main, _, _ = three
    monkeypatch.setattr(
        main, "connect", lambda: Failing("HTTP 404 (Not Found) reading marts/mart_rec_slate.parquet")
    )
    absent: list[str] = []
    assert erase._marts(main, "u-maya", ["mart_rec_slate"], absent) == []
    assert absent == main.mart_sources("mart_rec_slate")


def test_a_table_with_no_files_is_listed_and_one_that_cant_be_read_stops_the_erasure(three, monkeypatch):
    # Final review B2: a 404 while listing a table's files silently dropped it from the deletion.
    main, photos, users = three
    before = every_value(main)
    connect = main.connect
    ratings_src = main.sources("ratings")[0]

    def failing(message):
        def connect_(*a, **kw):
            con = connect(*a, **kw)

            class Wrapped:
                def execute(self, sql, *args):
                    if ratings_src in sql:
                        raise RuntimeError(message)
                    return con.execute(sql, *args)

            return Wrapped()

        return connect_

    monkeypatch.setattr(main, "connect", failing("IO Error: No files found that match the pattern"))
    p = erase.plan("maya", main, photos, users)
    assert ratings_src in p.absent and not any(f.startswith("ratings/") for f in p.files[main.url])
    for message in (
        "HTTP 404 (Not Found) error reading the file",
        "HTTP 500 server error",
        "connection reset",
    ):
        monkeypatch.setattr(main, "connect", failing(message))
        with pytest.raises(erase.EraseError, match="Nothing was deleted"):
            erase.plan("maya", main, photos, users)
    assert every_value(main) == before


def consent(users, uid, experiment, at):
    users.write("consents", f"consents/{uid}__{at:%Y%m%dT%H%M%S}.parquet",
                [{"user_id": uid, "terms_version": "x", "privacy_version": "x", "terms_at": at, "source": "settings",
                  "experiment": experiment, "logged_at": at}])  # fmt: skip


def test_delete_user_only_recs_deletes_the_tests_records_and_keeps_the_account(three):
    # Someone who asks to delete just their answers keeps their account (on, signed in), shots and bags. The
    # erasure is recorded first, with the rating ids that went and nothing else of them.
    main, photos, users = three
    on = datetime(2026, 10, 22, 7, tzinfo=UTC)
    users.write("users", "users/u-maya__20261022T070000.parquet",
                [{"user_id": "u-maya", "username": "maya", "role": "member", "disabled": False, "logged_at": on}])  # fmt: skip
    consent(users, "u-maya", True, on)
    p = erase.plan("maya", main, photos, users, only_recs=True)  # no turn-off needed
    assert p.marts == [] and p.files == {main.url: [
        "ratings/r-u-maya__20261020T070000.parquet", "ratings/r-u-maya__20261021T070000.parquet",
        "rec_responses/u-maya__slate-1__h__20261020T070000.parquet"]}  # fmt: skip
    done = erase.erase_recs(p, main, photos, users, now=datetime(2026, 10, 24, 7, tzinfo=UTC))
    assert done.files == p.files
    assert "u-maya" not in every_value(main.root / "ratings") + every_value(main.root / "rec_responses")
    assert "s-u-maya" in every_value(main) and "p-u-maya" in every_value(photos)
    (acct,) = [r for r in users.latest("users", "user_id") if r["user_id"] == "u-maya"]
    assert acct["disabled"] is False
    assert "r-owner" in every_value(main)
    (marker,) = [r for r in users.latest("recs_erasures", "user_id") if r["user_id"] == "u-maya"]
    assert marker["rating_ids"] == ["r-u-maya"] and "roaster.example" not in json.dumps(marker, default=str)
    # The full deletion takes the erasure record too.
    users.write("users", "users/u-maya__20261025T070000.parquet",
                [{"user_id": "u-maya", "username": "maya", "role": "member", "disabled": True,
                  "logged_at": datetime(2026, 10, 25, 7, tzinfo=UTC)}])  # fmt: skip
    full = erase.plan("maya", main, photos, users)
    assert "recs_erasures/u-maya__20261024T070000.parquet" in full.files[users.url]


def test_delete_user_refuses_when_a_datasets_folders_cant_be_listed(three, monkeypatch):
    # With a missing or expired HF_TOKEN a private dataset answers 404 and every table would look empty.
    main, photos, users = three
    monkeypatch.setattr("coffeedata.lake.remote_folders", lambda url: None)
    monkeypatch.setattr(users, "remote", True)
    for only_recs in (False, True):
        with pytest.raises(erase.EraseError, match="Couldn't list"):
            erase.plan("maya", main, photos, users, only_recs=only_recs)
    assert "u-maya" in every_value(main)
