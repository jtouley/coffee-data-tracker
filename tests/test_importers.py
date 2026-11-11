"""Weekly private importer snapshots (coffeedata.importers, importer_rules): the parsers on small saved pages, loud
failure on a changed layout, the raw and normalized layers, one immutable file per importer, week and layer, the kill
switch and purge, the private dataset, and that neither layer can reach anything public."""

from __future__ import annotations

import types
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest
import yaml

from coffeedata import importer_rules as rules
from coffeedata import importers
from coffeedata.http import PoliteClient
from coffeedata.lake import GLOBS, IMPORTER_TABLES, LAKE_TABLES, SCHEMAS, Lake

FIXTURES = Path(__file__).parent / "fixtures" / "importers"
CI_HTML = (FIXTURES / "cafeimports.html").read_text()
IA_HTML = (FIXTURES / "interamerican.html").read_text()
ALL_ON = dict.fromkeys(importers.IMPORTERS, True)
WED = date(2026, 11, 11)
MON = date(2026, 11, 9)  # its week
NOW = datetime(2026, 11, 11, 17, 37, 5, 123, tzinfo=UTC)


def test_cafe_imports_rows():
    rows = importers.parse_cafe_imports(CI_HTML)
    by_lot = {r["lot_id"]: r for r in rows}
    assert list(by_lot) == ["25973", "26784", "26453", "25748-2"]
    assert {k: v for k, v in by_lot["25973"].items() if k != "_raw"} == {
        "lot_id": "25973",
        "name": 'Asociación De Caficultores De Taipiplaya "ASOCAFE" (GrainPro)',
        "origin": "Bolivia",
        "region": "La Paz",
        "process": "Washed",
        "status": "spot",
        "est_arrival": None,
        "warehouse": None,
        "bags": 64,
        "bag_size": "69 Kg",
        "bag_kg": 69.0,
        "row": 0,
    }
    # the raw cells, verbatim: name and description, and those the normalized values come from
    assert by_lot["25973"]["_raw"]["Notes"].startswith("Graham with mild kahlua")
    assert set(by_lot["25973"]["_raw"]) <= set(importers.CAFE_IMPORTS_RAW)
    assert by_lot["26453"]["status"] == "at_origin" and by_lot["26453"]["est_arrival"] is None
    # an afloat lot: its estimated arrival as stated, without the status or label before it
    assert by_lot["25748-2"]["status"] == "afloat" and by_lot["25748-2"]["est_arrival"] == "Mid Nov 2026"
    assert by_lot["26784"]["est_arrival"] == "Early Jan 2027"


def test_interamerican_rows():
    rows = importers.parse_interamerican(IA_HTML)
    assert [(r["warehouse"], r["status"], r["bags"], r["bag_kg"]) for r in rows] == [
        ("Continental Terminals Annex", "afloat", 11, 59.0),
        ("Continental Terminals Annex", "in_store", 100, 60.0),
        ("Dupuy Houston", "afloat", 175, 59.0),
        ("Continental Terminals NJ", "afloat", 17, 1000.0),  # "1MT Supersacks"
    ]
    # nothing is inferred from the product name: the page has no lot, origin, region, process or arrival column
    assert all(r[k] is None for r in rows for k in ("lot_id", "origin", "region", "process", "est_arrival"))


def test_only_allowed_fields_are_stored():
    """No prices, links, page HTML or images in either layer: only the schemas' columns, and in the raw layer only
    the allowlisted cells' text."""
    for name, html in (("cafe_imports", CI_HTML), ("interamerican", IA_HTML)):
        raw, offers = importers.split(name, importers.IMPORTERS[name].parse(html), {})
        allowed = {"cafe_imports": importers.CAFE_IMPORTS_RAW, "interamerican": importers.INTERAMERICAN_RAW}[
            name
        ]
        for r in raw:
            assert set(r) <= {f.name for f in SCHEMAS["importer_raw"]}
            assert {c["column"] for c in r["cells"]} <= set(allowed)
            assert not any(s in c["text"] for c in r["cells"] for s in ("<", "http", "$", "Log In For Price"))
        for o in offers:
            assert set(o) <= {f.name for f in SCHEMAS["importer_offers"]}
            assert not any("$" in str(v) or "http" in str(v) for v in o.values())


@pytest.mark.parametrize(
    "html, match",
    [
        ("<html><body><p>We're updating our offerings.</p></body></html>", "no table with the columns"),
        # the table is there but rendered by the browser: zero rows is an error, never an empty snapshot
        (CI_HTML.split("<tbody>")[0] + "<tbody></tbody></table>", "zero rows parsed"),
        # a renamed column
        (CI_HTML.replace(">Bags</th>", ">Quantity</th>"), "no table with the columns"),
    ],
)
def test_changed_cafe_imports_layout_fails_loudly(html, match):
    with pytest.raises(importers.LayoutChanged, match=match):
        importers.parse_cafe_imports(html)


def test_changed_interamerican_layout_fails_loudly():
    with pytest.raises(importers.LayoutChanged, match="zero rows parsed"):
        importers.parse_interamerican(IA_HTML.split("<tbody>")[0] + "<tbody></tbody></table>")
    # most rows unreadable (bags no longer a number): fail rather than store a thinned list
    garbled = IA_HTML.replace('<td style="">11</td>', '<td style="">n/a</td>')
    with pytest.raises(importers.LayoutChanged, match="unreadable"):
        importers.parse_interamerican(garbled)


def test_field_readers():
    assert importers.bag_kg("60 Kg") == 60.0
    assert importers.bag_kg("59 Kg Bags") == 59.0
    assert importers.bag_kg("1MT Supersacks") == 1000.0
    assert importers.bag_kg("30 lbs") == 13.61
    assert importers.bag_kg("box") is None
    assert importers.status("IN STORE") == "in_store"
    assert importers.status("At origin") == "at_origin"
    assert importers.status("In Transit") == "in transit"  # an unseen value is kept as written, never dropped


def _client(pages: dict[str, str], calls: list[str]) -> PoliteClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return httpx.Response(200, text=pages[str(request.url)], headers={"content-type": "text/html"})

    return PoliteClient(min_interval=0, retries=1, transport=httpx.MockTransport(handler))


def _pages(ci: str = CI_HTML, ia: str = IA_HTML) -> dict[str, str]:
    return {importers.IMPORTERS["cafe_imports"].url: ci, importers.IMPORTERS["interamerican"].url: ia}


def test_snapshot_writes_one_file_per_importer_per_week_and_never_fetches_it_again(tmp_path):
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    calls: list[str] = []
    with _client(_pages(), calls) as client:
        first = importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=ALL_ON)
    assert [(r["importer"], r["status"], r.get("rows")) for r in first] == [
        ("cafe_imports", "stored", 4),
        ("interamerican", "stored", 4),
    ]
    path = tmp_path / "importers" / "offers" / "week=2026-11-09" / "importer=cafe_imports.parquet"
    t = pq.read_table(path).to_pylist()
    assert {r["week"] for r in t} == {date(2026, 11, 9)}  # the Monday of the fetch's week
    assert {r["fetched_at"] for r in t} == {NOW.replace(microsecond=0)}
    assert {r["importer"] for r in t} == {"cafe_imports"}
    assert {r["source_url"] for r in t} == {importers.IMPORTERS["cafe_imports"].url}
    pages = [c for c in calls if not c.endswith("/robots.txt")]
    assert len(pages) == 2
    # the raw layer beside it: the same lots, by row, with their cells verbatim
    raw = pq.read_table(
        tmp_path / "importers" / importers.path("cafe_imports", MON, "importer_raw")
    ).to_pylist()
    assert [(r["row"], r["lot_id"]) for r in raw] == [(r["row"], r["lot_id"]) for r in t]
    assert {"column": "Name", "text": "Gakenke - Kayanza - Washed (Ecotact)"} in raw[1]["cells"]
    assert [(r["process_type"], r["grower_type"]) for r in t] == [
        ("washed", "cooperative"),
        ("washed", None),
        ("natural", None),
        ("honey", "micromill"),
    ]

    # later the same week: nothing is fetched, nothing rewritten
    calls.clear()
    with _client(_pages(), calls) as client:
        again = importers.snapshot(lake, client, today=date(2026, 11, 15), now=lambda: NOW, switches=ALL_ON)
    assert [r["status"] for r in again] == ["skipped", "skipped"] and calls == []
    with pytest.raises(FileExistsError):  # immutable, as every lake file
        lake.write("importer_offers", importers.path("cafe_imports", date(2026, 11, 9)), t)


def test_a_changed_page_fails_that_importer_and_writes_nothing_for_it(tmp_path):
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    with _client(_pages(ci="<html><body>maintenance</body></html>"), []) as client:
        res = importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=ALL_ON)
    assert [r["status"] for r in res] == ["failed", "stored"]
    assert "LayoutChanged" in res[0]["error"]
    assert not (tmp_path / "importers" / importers.path("cafe_imports", date(2026, 11, 9))).exists()


def test_a_failed_fetch_fails_loudly(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return httpx.Response(403, text="no")

    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    with PoliteClient(min_interval=0, retries=1, transport=httpx.MockTransport(handler)) as client:
        res = importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=ALL_ON)
    assert [r["status"] for r in res] == ["failed", "failed"] and all("HTTP 403" in r["error"] for r in res)


def test_robots_disallow_is_respected(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    with PoliteClient(min_interval=0, retries=1, transport=httpx.MockTransport(handler)) as client:
        res = importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=ALL_ON)
    assert all(r["status"] == "failed" for r in res)
    assert set(calls) == {"/robots.txt"}  # never the page itself


def test_kill_switch(tmp_path, monkeypatch):
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    calls: list[str] = []
    switches = {"cafe_imports": False, "interamerican": True}
    with _client(_pages(), calls) as client:
        res = importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=switches)
    assert [r["status"] for r in res] == ["disabled", "stored"]
    assert not any("cafeimports" in c for c in calls)

    cfg = tmp_path / "importers.yaml"
    cfg.write_text("cafe_imports: {enabled: true}\ninteramerican: {enabled: false}\n")
    assert importers.enabled(cfg, "") == {"cafe_imports": True, "interamerican": False}
    assert importers.enabled(cfg, "cafe_imports") == {"cafe_imports": False, "interamerican": False}
    with pytest.raises(ValueError, match="names no importer"):
        importers.enabled(cfg, "cafe-imports")  # a mistyped switch is an error, not a no-op
    cfg.write_text("cafe_imports: {enabled: true}\n")
    with pytest.raises(ValueError, match="must list exactly"):
        importers.enabled(cfg, "")
    cfg.write_text("cafe_imports: {enabled: 'no'}\ninteramerican: {enabled: true}\n")
    with pytest.raises(ValueError, match="true or false"):
        importers.enabled(cfg, "")


def test_the_committed_config_lists_every_importer():
    cfg = yaml.safe_load(importers.CONFIG.read_text())
    assert set(cfg) == set(importers.IMPORTERS)
    assert importers.enabled(disabled_env="") == {n: cfg[n]["enabled"] for n in importers.IMPORTERS}


class FakeApi:
    def __init__(self, private: bool, existing: tuple[str, ...] = ()):
        self.private, self.existing, self.calls = private, set(existing), []

    def create_repo(self, repo_id, repo_type, private, exist_ok):
        self.calls.append(("create", repo_id, private, exist_ok))

    def dataset_info(self, repo_id):
        return types.SimpleNamespace(private=self.private)

    def get_paths_info(self, repo_id, paths, repo_type):
        return [types.SimpleNamespace(path=p, lfs=None, blob_id=None) for p in paths if p in self.existing]

    def upload_file(self, path_or_fileobj, path_in_repo, repo_id, repo_type, commit_message):
        self.calls.append(("card", path_in_repo))
        self.existing.add(path_in_repo)

    def list_repo_files(self, repo_id, repo_type):
        return sorted(self.existing)

    def super_squash_history(self, repo_id, repo_type, commit_message):
        self.calls.append(("squash",))

    def create_commit(self, repo_id, repo_type, operations, commit_message):
        from huggingface_hub import CommitOperationDelete

        self.calls.append(("commit", sorted(op.path_in_repo for op in operations)))
        for op in operations:
            if isinstance(op, CommitOperationDelete):
                self.existing.discard(op.path_in_repo)
            else:
                self.existing.add(op.path_in_repo)


def _remote(tmp_path, monkeypatch, api: FakeApi) -> Lake:
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", lambda token=None: api)
    monkeypatch.setattr(importers, "HUB_RETRY_WAITS", ())
    for var in importers.OTHER_DATASETS:
        monkeypatch.delenv(var, raising=False)
    return Lake("hf://datasets/someone/importers", staging=tmp_path / "staging", tables=IMPORTER_TABLES)


def test_remote_dataset_is_created_private_and_written_once_per_week(tmp_path, monkeypatch):
    api = FakeApi(private=True)
    lake = _remote(tmp_path, monkeypatch, api)
    with _client(_pages(), []) as client:
        res = importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=ALL_ON)
    assert [r["status"] for r in res] == ["stored", "stored"]
    assert api.calls[:2] == [("create", "someone/importers", True, True), ("card", "README.md")]
    commits = [c[1] for c in api.calls if c[0] == "commit"]
    schemas = [
        "offers/week=0000-00-00/importer=schema.parquet",
        "raw/week=0000-00-00/importer=schema.parquet",
    ]
    # one commit per importer, holding both layers: a week has both or neither
    assert commits[0] == sorted(
        [*schemas, importers.path("cafe_imports", MON), importers.path("cafe_imports", MON, "importer_raw")]
    )
    assert commits[1] == sorted(
        [importers.path("interamerican", MON), importers.path("interamerican", MON, "importer_raw")]
    )
    assert lake.staged_files() == sorted(lake.root / s for s in schemas)
    assert sum(c == ("card", "README.md") for c in api.calls) == 1  # sent once; unchanged since

    calls: list[str] = []
    with _client(_pages(), calls) as client:  # a fresh runner later that week: the dataset already has it
        again = importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=ALL_ON)
    assert [r["status"] for r in again] == ["skipped", "skipped"] and calls == []


def test_refuses_a_public_dataset(tmp_path, monkeypatch):
    api = FakeApi(private=False)
    lake = _remote(tmp_path, monkeypatch, api)
    calls: list[str] = []
    with _client(_pages(), calls) as client, pytest.raises(RuntimeError, match="not private"):
        importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=ALL_ON)
    assert calls == [] and not any(c[0] == "commit" for c in api.calls)


@pytest.mark.parametrize("var", importers.OTHER_DATASETS)
def test_refuses_the_lake_or_any_other_dataset(tmp_path, monkeypatch, var):
    api = FakeApi(private=True)
    lake = _remote(tmp_path, monkeypatch, api)
    monkeypatch.setenv(var, "hf://datasets/someone/importers@main")
    with pytest.raises(RuntimeError, match=f"same dataset as {var}"):
        importers.ensure_private(lake)
    assert api.calls == []


def test_purge_removes_a_switched_off_importer_everywhere(tmp_path, monkeypatch):
    api = FakeApi(private=True)
    lake = _remote(tmp_path, monkeypatch, api)
    with _client(_pages(), []) as client:
        importers.snapshot(lake, client, today=WED, now=lambda: NOW, switches=ALL_ON)
        importers.snapshot(lake, client, today=date(2026, 11, 18), now=lambda: NOW, switches=ALL_ON)
    with pytest.raises(RuntimeError, match="still switched on"):
        importers.purge(lake, "cafe_imports", switches=ALL_ON)
    off = ALL_ON | {"cafe_imports": False}
    listed = importers.purge(lake, "cafe_imports", switches=off, dry_run=True)
    assert listed == sorted(
        importers.path("cafe_imports", w, t)
        for w in (date(2026, 11, 9), date(2026, 11, 16))
        for t in importers.LAYERS
    )
    assert importers.purge(lake, "cafe_imports", switches=off) == listed
    assert not any("cafe_imports" in f for f in api.existing)
    assert any("interamerican" in f for f in api.existing)  # the other source is untouched
    assert api.calls[-1] == ("squash",)  # and the removed text can't be read from an old revision


def test_neither_layer_can_reach_anything_public():
    """The importer tables live only in their own dataset: not main-lake tables (so dbt's lake roots and macro never
    name them), not referenced by any dbt model, contract, export, the site or the MCP server, and the release's
    public tables are the four public_* ones."""
    from coffeedata import public

    assert not set(IMPORTER_TABLES) & set(LAKE_TABLES)
    assert Lake.importers.__doc__ and "Never a dbt source" in Lake.importers.__doc__
    assert public.PUBLIC_TABLES == [
        "public_coffee",
        "public_roaster",
        "public_price_event",
        "public_market_weekly",
    ]
    root = Path(__file__).parent.parent
    folders = {GLOBS[t].split("/")[0] for t in IMPORTER_TABLES}
    macro = (root / "dbt/macros/lake.sql").read_text()
    assert not any(f"'{f}/" in macro for f in folders)
    sources = [
        *(root / "dbt").rglob("*.sql"),
        *(root / "dbt").rglob("*.yml"),
        *(root / "contracts").rglob("*.*"),
        *(root / "marketplace").rglob("*.*"),
        *(root / "src/coffeedata/templates").rglob("*.*"),
        *(
            root / "src/coffeedata" / f
            for f in ("public.py", "site.py", "snowflake_load.py", "mcp_server.py")
        ),
    ]
    for f in sources:
        if f.is_file() and "__pycache__" not in f.parts:
            text = f.read_text(errors="ignore").lower()
            assert "importer" not in text and "coffeedata_importers" not in text, f
    # the weekly build reads the main lake only
    cli = (root / "src/coffeedata/cli.py").read_text()
    assert (
        "Lake.importers()" in cli and cli.count("Lake.importers()") == 2
    )  # snapshot and purge, nothing else


def test_rules_decide_or_abstain():
    # process: the catalog's model (wet-hulled counts as honey; anaerobic wins); abstain on decaf or a mix
    assert rules.process("Natural", "Karana - Carbonic Maceration - Anaerobic - Natural") == "anaerobic"
    assert rules.process("Wet-Hulled", "Sumatra Lintong") == "honey"
    assert rules.process("Decaf", "Mountain Water Process") is None
    assert rules.process(None, "Costa Rica Sonora Honey Natural") is None
    assert rules.process(None, "Brazil Cerrado 17/18 Natrual") is None  # a misspelling: abstain, don't guess
    assert rules.process(None, "Ethiopia Yirgacheffe G2 Washed") == "washed"
    # varieties, in the catalog's spelling; Java is the island on an Indonesian lot
    assert rules.varieties("Natural - Fazenda Santa Lucia - Yellow Bourbon (GrainPro)") == ["Yellow Bourbon"]
    assert rules.varieties("Indonesia Java G1") is None
    assert rules.varieties("Washed (GrainPro)") is None
    # altitude and harvest only when stated
    assert rules.altitude("grown at 1,700-1,900 masl") == (1700, 1900)
    assert rules.altitude("Screen 16+") == (None, None)
    assert rules.harvest_months("Harvest: Nov - Feb") == ["nov", "dec", "jan", "feb"]
    assert rules.harvest_months("Graham with mild kahlua") is None
    # grower: a part of the name with a grower word; a placeholder keeps the type but not the name
    assert rules.grower("Natural - Fazenda Santa Lucia - Yellow Bourbon (GrainPro)") == (
        "farm",
        "Fazenda Santa Lucia",
    )
    assert rules.grower("Natural - Fazenda TBC (SC Bags)") == ("farm", None)
    assert rules.grower("Gakenke - Kayanza - Washed (Ecotact)") == (None, None)
    assert rules.grower("Guatemala Huehuetenango ASDECAFE SHB EP Organic") == (None, None)
    # certifications: Cafe Imports marks absence (nft, norg); a claim and a denial together: abstain
    assert rules.certifications("nft,norg,Microlot,Microlot nft,norg,", states_absence=True) == []
    fto = "Fair Trade,Fairtrade,Organic,FTO Norte FTO Fair Trade,Fairtrade,Organic,FTO"
    assert rules.certifications(fto, states_absence=True) == ["organic", "fair_trade"]
    assert rules.certifications("nft,Organic,Organic La Paz nft,norg,", states_absence=True) is None
    assert rules.certifications("Colombia Popayan Supremo", states_absence=False) is None


def test_cli_exits_1_when_an_importer_failed(tmp_path, monkeypatch, capsys):
    from coffeedata import cli

    monkeypatch.setenv("COFFEEDATA_IMPORTERS", str(tmp_path / "importers"))
    monkeypatch.setattr(
        importers,
        "snapshot",
        lambda *a, **k: [{"importer": "cafe_imports", "status": "failed", "error": "x"}],
    )
    with pytest.raises(SystemExit) as e:
        cli.main(["importer-snapshot", "--run-date", "2026-11-11"])
    assert e.value.code == 1 and '"failed"' in capsys.readouterr().out


def test_workflow_runs_on_main_only_and_never_on_pull_requests():
    """Its job holds a Hub write token: like eval-floors, a schedule and dispatch only, and the job runs on main."""
    wf = yaml.safe_load((Path(__file__).parent.parent / ".github/workflows/importers.yml").read_text())
    assert set(wf[True]) == {"schedule", "workflow_dispatch"}  # YAML 1.1 reads `on:` as True
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        assert job.get("if") == "github.ref == 'refs/heads/main'", name
        assert job["steps"][0]["with"]["persist-credentials"] is False
        # the token goes to the one step that needs it, never to the whole job
        assert "HF_TOKEN" not in str(job.get("env", {}))
        holders = [s for s in job["steps"] if "HF_TOKEN" in str(s.get("env", {}))]
        assert len(holders) == 1 and "importer-snapshot" in holders[0]["run"]
