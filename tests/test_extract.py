from types import SimpleNamespace

from coffeedata import extract
from coffeedata.models import CatalogItem, Enrichment


def item(title, tags=(), body=""):
    return CatalogItem(
        roaster_id="r",
        product_id="1",
        handle="h",
        url="u",
        title=title,
        platform="shopify",
        tags=list(tags),
        body_text=body,
    )


def test_structured_tags_win():
    e = extract.rules(
        item(
            "Panama Finca Deborah Gesha Washed",
            tags=["origin:Panama", "process:Anaerobic Washed", "caffeine:full"],
        )
    )
    assert e.origin_country == "Panama" and e.process == "anaerobic" and "Gesha" in e.varietals


def test_shopify_double_underscore_tags_and_body():
    e = extract.rules(
        item(
            "Iridescent",
            tags=["roastlevel__medium-roast", "origin__blend"],
            body="Tasting notes: Milk chocolate, cherry & a hint of almond.",
        )
    )
    assert e.roast_level == "medium" and e.is_blend
    assert e.tasting_notes == ["milk chocolate", "cherry", "almond"]


def test_demonym_and_elevation():
    e = extract.rules(
        item("Muthonjo", body="One of the best Kenyan coffees. Grown at 1,700-1,900 masl. Process: Washed")
    )
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
    text = (
        '{"origin_country": null, "origin_region": "Nyeri", "producer": null, "process": "washed", '
        '"varietals": ["SL28"], "elevation_masl_min": null, "elevation_masl_max": null, "roast_level": null, '
        '"tasting_notes": ["Blackcurrant", "Grapefruit notes"], "is_blend": false, "is_decaf": false}'
    )
    msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])
    e = extract.parse_llm_message(msg, base)
    assert e.origin_country == "Kenya" and e.origin_region == "Nyeri" and e.process == "washed"
    assert e.tasting_notes == ["blackcurrant", "grapefruit"] and e.method == "llm"
    refused = SimpleNamespace(stop_reason="refusal", content=[])
    assert extract.parse_llm_message(refused, base) is None


def test_java_is_an_origin_only_with_a_javanese_place():
    assert extract.rules(item("Mocha Java Blend")).origin_country is None
    mocha = extract.rules(item("Moka Java", body="Our take on the oldest blend."))
    assert mocha.is_blend and mocha.origin_country is None
    assert extract.rules(item("Java Chip", body="Chocolate and roast.")).origin_country is None
    assert extract.rules(item("West Java Frinsa Natural")).origin_country == "Indonesia"
    assert extract.rules(item("Java Estate Kayumas")).origin_country == "Indonesia"
    assert extract.rules(item("Sumatra Takengon")).origin_country == "Indonesia"


def test_grab_bags_and_multi_country_titles_have_no_origin():
    multi = item("5 lb Single Origin Coffee — Puerto Rico Supremo, Mexico, or Colombia")
    assert extract.rules(multi).origin_country is None
    pick = item("Roaster Dave's Pick (Random Coffee)", body="Could be a Kona, could be a Kenya.")
    assert extract.rules(pick).origin_country is None
    assert extract.rules(item("Kona Extra Fancy")).origin_country == "United States"
    tagged = item("Colombia & Ethiopia", tags=["origin:Colombia"])
    assert extract.rules(tagged).origin_country == "Colombia"


# Counter Culture's Finca El Puente lots, all from Marcala, Honduras. The bodies put the place far below the story.
_EL_PUENTE_STORY = (
    "We met Marysabel Caballero and Moisés Herrera, the owners of Finca El Puente, in 2005. They have become like "
    "family to Counter Culture, and their constant pursuit of coffee perfection inspires us year after year. "
) * 12


def test_el_puente_honey_finds_the_country_far_into_the_body():
    body = _EL_PUENTE_STORY + "Tasting notes: blackberry, chocolate, apricot.\nRegion: Marcala, Honduras"
    assert len(_EL_PUENTE_STORY) > 1500
    e = extract.rules(item("Finca El Puente - Honey Processed", tags=["origin__single-origin"], body=body))
    assert e.origin_country == "Honduras" and e.process == "honey"


def test_el_puente_anaerobic_names_only_the_region():
    body = (
        "This anaerobic lot was fermented in sealed tanks for 72 hours at Finca El Puente, high above the town of "
        "Marcala. Moisés and Marysabel have pushed the limits of processing for years."
    )
    e = extract.rules(item("Finca El Puente - Anaerobic", tags=["origin__single-origin"], body=body))
    assert e.origin_country == "Honduras" and e.process == "anaerobic"


def test_el_puente_sl28_variety_history_is_not_the_origin():
    body = (
        "SL-28 is a variety developed in Kenya in the 1930s by Scott Laboratories, prized for its drought tolerance "
        "and its bright, juicy cup. Kenya remains its best-known home. This lot was grown at Finca El Puente in "
        "Marcala, Honduras, where Moisés and Marysabel planted it a decade ago."
    )
    e = extract.rules(item("Finca El Puente SL-28 Variety", tags=["origin__single-origin"], body=body))
    assert e.origin_country == "Honduras"
    # The same story without the country still lands in Honduras, by its region.
    no_country = body.replace(", Honduras", "")
    assert extract.rules(item("Finca El Puente SL-28 Variety", body=no_country)).origin_country == "Honduras"


def test_variety_origin_phrases_never_count():
    for phrase in ("originally from Ethiopia", "native to southwestern Ethiopia", "bred in Ethiopia"):
        body = f"Gesha, a variety {phrase}, grown at Hacienda La Esmeralda in Boquete. Juicy and floral."
        assert extract.rules(item("Esmeralda Gesha", body=body)).origin_country == "Panama", phrase


def test_region_only_body():
    body = "Grown by smallholders around Yirgacheffe and washed at the Konga station. Notes: jasmine, lemon."
    assert extract.rules(item("Konga", body=body)).origin_country == "Ethiopia"
    assert (
        extract.rules(item("Finca La Esperanza", body="From Huila's high slopes.")).origin_country
        == "Colombia"
    )
    assert extract.rules(item("La Esperanza", tags=["region:Nariño"])).origin_country == "Colombia"
    # Region names only count where they can mean one place.
    assert extract.rules(item("House Blend", body="Roasted in Santa Barbara, CA.")).origin_country is None
    assert extract.rules(item("Islander", body="Inspired by Antigua and Barbuda.")).origin_country is None


def test_true_kenyan_sl28_stays_kenya():
    body = (
        "SL-28 and SL-34, the varieties Kenya is famous for, grown by members of the Othaya cooperative in Nyeri, "
        "Kenya. Washed and dried on raised beds. Notes: blackcurrant, grapefruit."
    )
    assert extract.rules(item("Gatuyaini AA SL-28", body=body)).origin_country == "Kenya"
    assert extract.rules(item("Kenya Kiambu SL28", body="SL28 developed in Kenya.")).origin_country == "Kenya"
    # Only a variety beside the country, nothing else: the country is still the best guess.
    assert extract.rules(item("Gichathaini", body="Classic SL28 from Kenya.")).origin_country == "Kenya"


def test_the_country_named_as_origin_beats_the_first_named():
    body = "Like the best coffees of Ethiopia, this one is floral. Origin: Rwanda. Washed at the Huye Mountain station."
    assert extract.rules(item("Huye Mountain", body=body)).origin_country == "Rwanda"
    location = "Farm: Don Andres\nLocation: Las Flores, Honduras\nProcess: Honey"
    assert extract.rules(item("Nahun Fernandez", body=location)).origin_country == "Honduras"
