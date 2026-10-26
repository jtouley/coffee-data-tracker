from types import SimpleNamespace

from coffeedata import extract
from coffeedata.models import CatalogItem, Enrichment


def item(title, tags=(), body=""):
    return CatalogItem(roaster_id="r", product_id="1", handle="h", url="u", title=title, platform="shopify",
                       tags=list(tags), body_text=body)


def test_structured_tags_win():
    e = extract.rules(item("Panama Finca Deborah Gesha Washed",
                           tags=["origin:Panama", "process:Anaerobic Washed", "caffeine:full"]))
    assert e.origin_country == "Panama" and e.process == "anaerobic" and "Gesha" in e.varietals


def test_shopify_double_underscore_tags_and_body():
    e = extract.rules(item("Iridescent", tags=["roastlevel__medium-roast", "origin__blend"],
                           body="Tasting notes: Milk chocolate, cherry & a hint of almond."))
    assert e.roast_level == "medium" and e.is_blend
    assert e.tasting_notes == ["milk chocolate", "cherry", "almond"]


def test_demonym_and_elevation():
    e = extract.rules(item("Muthonjo", body="One of the best Kenyan coffees. Grown at 1,700-1,900 masl. Process: Washed"))
    assert e.origin_country == "Kenya" and e.process == "washed"
    assert (e.elevation_masl_min, e.elevation_masl_max) == (1700, 1900)


def test_needs_llm():
    assert extract.needs_llm(Enrichment())
    assert not extract.needs_llm(Enrichment(origin_country="Kenya", process="washed", tasting_notes=["fig"]))


def test_llm_request_shape():
    req = extract.llm_request("X", ["a"], "y" * 10_000)
    assert req["model"] == "claude-haiku-5-5"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert len(req["messages"][0]["content"]) < 4200


def test_parse_llm_message_merges_over_rules():
    base = Enrichment(origin_country="Kenya", roast_level="light")
    text = ('{"origin_country": null, "origin_region": "Nyeri", "producer": null, "process": "washed", '
            '"varietals": ["SL28"], "elevation_masl_min": null, "elevation_masl_max": null, "roast_level": null, '
            '"tasting_notes": ["Blackcurrant", "Grapefruit notes"], "is_blend": false, "is_decaf": false}')
    msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])
    e = extract.parse_llm_message(msg, base)
    assert e.origin_country == "Kenya" and e.origin_region == "Nyeri" and e.process == "washed"
    assert e.tasting_notes == ["blackcurrant", "grapefruit"] and e.method == "llm"
    refused = SimpleNamespace(stop_reason="refusal", content=[])
    assert extract.parse_llm_message(refused, base) is None
