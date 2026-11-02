"""Findability of the public data: the site's schema.org Dataset (JSON-LD), robots.txt and sitemap.xml, and releases
that describe themselves (manifest.json, and a `release-<run date>` tag on a Hugging Face export). No dbt needed: the
export tests use a small hand-made database with the four public tables."""

import hashlib
import json
import re
import types
from datetime import date

import duckdb
import pytest

from coffeedata import public, site

RELEASE = {"release_build_id": "20261103T061700-abc123", "release_run_date": "2026-11-02"}
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def google_dataset_problems(doc: dict) -> list[str]:
    """What Google's Dataset structured data requires or expects
    (https://developers.google.com/search/docs/appearance/structured-data/dataset), checked on one JSON-LD object."""
    problems = []
    if doc.get("@context") not in ("https://schema.org/", "https://schema.org", "http://schema.org/"):
        problems.append("@context is not schema.org")
    if doc.get("@type") != "Dataset":
        problems.append("@type is not Dataset")
    if not isinstance(doc.get("name"), str) or not doc["name"]:
        problems.append("name (required) is missing")
    if not isinstance(doc.get("description"), str) or not 50 <= len(doc["description"]) <= 5000:
        problems.append("description (required) must be 50 to 5000 characters")
    for key in ("url", "license"):
        if not str(doc.get(key, "")).startswith("https://"):
            problems.append(f"{key} is not an https URL")
    for who in [doc.get("creator")] if isinstance(doc.get("creator"), dict) else doc.get("creator") or []:
        if who.get("@type") not in ("Person", "Organization") or not who.get("name"):
            problems.append("creator needs a @type of Person or Organization and a name")
    if not doc.get("creator"):
        problems.append("creator is missing")
    tc = doc.get("temporalCoverage", "")
    start, _, end = tc.partition("/")
    if not (ISO_DATE.fullmatch(start) and (end == ".." or ISO_DATE.fullmatch(end))):
        problems.append(f"temporalCoverage {tc!r} is not an ISO 8601 interval")
    sc = doc.get("spatialCoverage")
    if not (isinstance(sc, str) or (isinstance(sc, dict) and sc.get("@type") == "Place" and sc.get("name"))):
        problems.append("spatialCoverage is not text or a Place")
    for v in doc.get("variableMeasured", []):
        if not (isinstance(v, str) or (v.get("@type") == "PropertyValue" and v.get("name"))):
            problems.append(f"variableMeasured {v} is not text or a named PropertyValue")
    for d in doc.get("distribution", []):
        if d.get("@type") != "DataDownload" or not str(d.get("contentUrl", "")).startswith("https://"):
            problems.append(f"distribution {d} is not a DataDownload with an https contentUrl")
        if not d.get("encodingFormat"):
            problems.append(f"distribution {d.get('contentUrl')} has no encodingFormat")
    if "dateModified" in doc and not ISO_DATE.fullmatch(str(doc["dateModified"])):
        problems.append("dateModified is not an ISO 8601 date")
    if "version" in doc and not isinstance(doc["version"], (str, int, float)):
        problems.append("version is not text or a number")
    if not isinstance(doc.get("isAccessibleForFree", True), bool):
        problems.append("isAccessibleForFree is not a boolean")
    return problems


def _links(export: str = "") -> dict:
    return site.links(export, "https://registry.coffee")


@pytest.mark.parametrize("export", ["", "hf://datasets/jtouley/us-coffee-market"])
def test_the_dataset_jsonld_meets_googles_requirements(export):
    doc = site.dataset_jsonld(RELEASE, _links(export))
    assert google_dataset_problems(doc) == []
    assert doc["dateModified"] == "2026-11-02" and doc["version"] == "20261103T061700-abc123"
    assert doc["temporalCoverage"] == "2026-10-26/.."
    assert doc["spatialCoverage"] == {"@type": "Place", "name": "United States"}
    assert doc["license"] == "https://github.com/jtouley/coffee-data-tracker/blob/main/LICENSE-DATA"
    assert doc["name"] == public.contract()["name"]
    urls = [d["contentUrl"] for d in doc["distribution"]]
    assert urls[0] == "https://registry.coffee/catalog/catalog.json"
    if export:
        assert doc["sameAs"] == ["https://huggingface.co/datasets/jtouley/us-coffee-market"]
        assert urls[1:] == [
            f"https://huggingface.co/datasets/jtouley/us-coffee-market/resolve/main/data/{t}.parquet"
            for t in public.PUBLIC_TABLES
        ]
    else:  # no Hugging Face dataset is published: none is claimed
        assert urls == urls[:1] and "sameAs" not in doc


def test_the_dataset_jsonld_measures_every_contract_column():
    contract = public.contract()
    columns = {p["name"] for t in contract["schema"] for p in t["properties"]}
    doc = site.dataset_jsonld(RELEASE, _links())
    assert {v["name"] for v in doc["variableMeasured"]} == columns
    assert all(v.get("description") for v in doc["variableMeasured"])


def test_a_build_without_a_release_leaves_out_its_dates():
    doc = site.dataset_jsonld({"release_build_id": None, "release_run_date": None}, _links())
    assert "dateModified" not in doc and "version" not in doc
    assert google_dataset_problems(doc) == []


def test_units_in_the_contract_reach_the_jsonld():
    contract = public.contract()
    prop = contract["schema"][0]["properties"][0]
    prop["customProperties"] = [{"property": "unit", "value": "USD"}]
    doc = site.dataset_jsonld(RELEASE, _links(), contract)
    assert doc["variableMeasured"][0]["unitText"] == "USD"


def test_the_jsonld_is_data_and_leaves_the_csp_alone():
    """A `<script type="application/ld+json">` never runs, so with_csp hashes only the page's bare scripts; and no
    text in the JSON can close the tag."""
    doc = {"@type": "Dataset", "name": "x</script><script>alert(1)</script>", "description": "a & b"}
    block = site.jsonld_script(doc)
    assert block.count("</script>") == 1 and "<script>" not in block
    assert (
        json.loads(block.removeprefix('<script type="application/ld+json">').removesuffix("</script>")) == doc
    )
    js = "console.log(1)"
    page = f'<head><meta charset="utf-8">{block}</head><body><script>{js}</script></body>'
    csp = re.search(r"script-src ([^;]*);", site.with_csp(page)).group(1)
    assert (
        csp
        == "'sha256-" + __import__("base64").b64encode(hashlib.sha256(js.encode()).digest()).decode() + "'"
    )


def test_robots_allows_everything_and_points_at_the_sitemap():
    assert site.robots_txt("https://registry.coffee") == (
        "User-agent: *\nAllow: /\n\nSitemap: https://registry.coffee/sitemap.xml\n"
    )
    xml = site.sitemap_xml("https://registry.coffee", {"": "2026-11-02", "terms/": "2026-10-27"})
    import xml.etree.ElementTree as ET

    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = ET.fromstring(xml).findall("s:url", ns)
    assert [u.findtext("s:loc", namespaces=ns) for u in urls] == [
        "https://registry.coffee/",
        "https://registry.coffee/catalog/",
        "https://registry.coffee/privacy/",
        "https://registry.coffee/terms/",
    ]
    assert [u.findtext("s:lastmod", namespaces=ns) for u in urls] == ["2026-11-02", None, None, "2026-10-27"]


# --- self-describing releases ------------------------------------------------------------------------


def _public_db(path, build_id="20261103T061700-abc123", run_date="2026-11-02"):
    """The four public tables, a row or two each, with their release columns."""
    con = duckdb.connect(str(path))
    rel = f"'{build_id}' as release_build_id, date '{run_date}' as release_run_date"
    con.execute(
        f"create table public_coffee as select * from (values ('a.com:1', date '{run_date}'), "
        f"('a.com:2', date '{run_date}')) t(coffee_id, last_seen), (select {rel})"
    )
    con.execute(f"create table public_roaster as select 'a.com' as roaster_id, {rel}")
    con.execute(
        f"create table public_price_event as select date '{run_date}' as run_date, 'a.com:1' as coffee_id, "
        f"'v1' as variant_id, 'new' as event, {rel}"
    )
    con.execute(
        f"create table public_market_weekly as select 3 as coffees, 'Kenya' as origin_country, 'washed' as process, {rel}"
    )
    con.close()
    return path


class FakeHf:
    """huggingface_hub.HfApi, recording commits and tags; `tags` are the tags the repo already has."""

    tags: list[str] = []
    calls: list = []

    def __init__(self, token=None):
        pass

    def create_repo(self, repo_id, repo_type, private, exist_ok):
        FakeHf.calls.append(("repo", repo_id))

    def create_commit(self, repo_id, repo_type, operations, commit_message):
        FakeHf.calls.append(("commit", sorted(op.path_in_repo for op in operations), commit_message))
        return types.SimpleNamespace(oid="c0ffee")

    def list_repo_refs(self, repo_id, repo_type):
        return types.SimpleNamespace(tags=[types.SimpleNamespace(name=t) for t in FakeHf.tags])

    def create_tag(self, repo_id, *, tag, tag_message, revision, repo_type, exist_ok):
        FakeHf.calls.append(("tag", repo_id, tag, revision, repo_type))
        FakeHf.tags.append(tag)


@pytest.fixture
def fake_hf(monkeypatch, tmp_path):
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeHf)
    monkeypatch.chdir(tmp_path)  # an hf:// export stages its files in build/public
    FakeHf.tags, FakeHf.calls = [], []
    return FakeHf


def test_a_directory_export_writes_a_self_describing_manifest(tmp_path):
    db = _public_db(tmp_path / "p.duckdb")
    out = public.export(db, str(tmp_path / "out"))
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert out["dest"] == str(tmp_path / "out") and "tag" not in out
    assert manifest["release_build_id"] == "20261103T061700-abc123"
    assert manifest["release_run_date"] == manifest["as_of_run"] == "2026-11-02"
    assert manifest["contract"] == {
        "id": "coffee-data-public-catalog",
        "version": str(public.contract()["version"]),
    }
    assert manifest["license"] == public.DATA_LICENSE
    assert manifest["tables"] == {
        "public_coffee": 2,
        "public_roaster": 1,
        "public_price_event": 1,
        "public_market_weekly": 1,
    }
    for name, f in manifest["files"].items():
        assert f["sha256"] == hashlib.sha256((tmp_path / "out" / name).read_bytes()).hexdigest()
        assert f["rows"] == manifest["tables"][name.removesuffix(".parquet")]


def test_a_hugging_face_export_is_tagged_with_its_run_date(tmp_path, fake_hf):
    db = _public_db(tmp_path / "p.duckdb")
    out = public.export(db, "hf://datasets/jtouley/us-coffee-market")
    assert out["tag"] == "release-2026-11-02"
    kinds = [c[0] for c in fake_hf.calls]
    assert kinds == ["repo", "commit", "tag"]
    commit = fake_hf.calls[1]
    assert "manifest.json" in commit[1] and "data/public_coffee.parquet" in commit[1]
    assert commit[2] == "weekly public export (run 2026-11-02, build 20261103T061700-abc123)"
    # The tag is on the export's own commit, so @release-2026-11-02 reads exactly these files.
    assert fake_hf.calls[2] == ("tag", "jtouley/us-coffee-market", "release-2026-11-02", "c0ffee", "dataset")
    uploaded = json.loads((tmp_path / "build" / "public" / "manifest.json").read_text())
    assert uploaded["release_build_id"] == "20261103T061700-abc123" and uploaded["files"]


def test_a_second_export_of_the_same_run_keeps_the_existing_tag(tmp_path, fake_hf):
    db = _public_db(tmp_path / "p.duckdb")
    public.export(db, "hf://datasets/jtouley/us-coffee-market")
    fake_hf.calls.clear()
    out = public.export(db, "hf://datasets/jtouley/us-coffee-market")
    assert out["tag"] == "release-2026-11-02"
    assert [c[0] for c in fake_hf.calls] == ["repo", "commit"]  # committed, not re-tagged
    assert fake_hf.tags == ["release-2026-11-02"]


def test_a_new_run_gets_its_own_tag(tmp_path, fake_hf):
    public.export(_public_db(tmp_path / "a.duckdb"), "hf://datasets/jtouley/us-coffee-market")
    public.export(
        _public_db(tmp_path / "b.duckdb", "20261110T061700-def456", "2026-11-09"),
        "hf://datasets/jtouley/us-coffee-market",
    )
    assert fake_hf.tags == ["release-2026-11-02", "release-2026-11-09"]


def test_an_export_without_a_release_falls_back_to_the_run_it_checked(tmp_path, fake_hf):
    """A plain `dbt build` has no release columns set: the manifest says so, and the tag is the latest run."""
    con = duckdb.connect(str(_public_db(tmp_path / "p.duckdb")))
    for t in public.PUBLIC_TABLES:
        con.execute(f"update {t} set release_build_id = null, release_run_date = null")
    con.close()
    out = public.export(tmp_path / "p.duckdb", "hf://datasets/a/b")
    assert out["release_build_id"] is None and out["release_run_date"] is None
    assert out["tag"] == "release-2026-11-02"
    assert fake_hf.calls[1][2] == "weekly public export (run 2026-11-02)"


def test_release_tag_needs_a_run():
    assert public.release_tag({"release_run_date": None, "as_of_run": None}) is None
    assert public.release_tag({"release_run_date": str(date(2026, 11, 2))}) == "release-2026-11-02"


def test_the_licence_text_travels_with_the_export(tmp_path, fake_hf, monkeypatch):
    """CDLA-Permissive-2.0 asks that the licence text go with shared data: LICENSE-DATA is in a directory export and
    in the Hugging Face commit when the repository has it, and quietly left out when it doesn't."""
    lic = tmp_path / "LICENSE-DATA-src"
    lic.write_text("Community Data License Agreement - Permissive - Version 2.0\n")
    monkeypatch.setattr(public, "LICENSE_FILE", lic)
    db = _public_db(tmp_path / "p.duckdb")
    public.export(db, str(tmp_path / "out"))
    assert (tmp_path / "out" / "LICENSE-DATA").read_text() == lic.read_text()
    public.export(db, "hf://datasets/jtouley/us-coffee-market")
    assert "LICENSE-DATA" in fake_hf.calls[1][1]

    monkeypatch.setattr(public, "LICENSE_FILE", tmp_path / "missing")  # the working directory has none either
    fake_hf.calls.clear()
    public.export(db, str(tmp_path / "out"))
    assert not (tmp_path / "out" / "LICENSE-DATA").exists()  # nor one left from the export before
    public.export(db, "hf://datasets/jtouley/us-coffee-market")
    assert "LICENSE-DATA" not in fake_hf.calls[1][1] and "manifest.json" in fake_hf.calls[1][1]
