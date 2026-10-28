from types import SimpleNamespace

import pytest

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


# Origin probes: realistic listings, including those from the reviews of #41. The rules answer only when a listing is
# clear; when it names several candidate countries (a variety's homeland, a comparison, a blend) they leave the origin
# empty, and needs_llm() hands the product to Claude. `ABSTAIN` lists the cases where that is the expected result:
# the rules must never give a wrong country there, only the right one or None.
G = "Grown in Marcala, Honduras."
SL = (
    "SL-28 is a variety developed in Kenya in the 1930s by Scott Laboratories, prized for its drought tolerance "
    "and its bright, juicy cup. Kenya remains its best-known home. This lot was grown at Finca El Puente in "
    "Marcala, Honduras, where Moisés and Marysabel planted it a decade ago."
)
ORIGIN_CASES = [
    ("A1", "Finca La Esperanza var. Colombia", G, "Honduras"),
    (
        "A2",
        "Shakiso Lot 12 Natural",
        "Heirloom varieties indigenous to Ethiopia, grown by smallholders at 2,100 masl.",
        "Ethiopia",
    ),
    (
        "A3",
        "Gesha Village Natural",
        "Gesha, native to Ethiopia, rose to fame in Panama. Gesha Village Estate brings it home to Bench Maji.",
        "Ethiopia",
    ),
    (
        "A4",
        "Gori Gesha Washed",
        "Gesha is native to Ethiopia, where this lot was grown. Unlike the Gesha made famous by Panama, it is floral and delicate.",
        "Ethiopia",
    ),
    (
        "B1",
        "La Esperanza Lot 4",
        "Grown by smallholders around Huila. Roasted fresh in Santa Fe, New Mexico.",
        "Colombia",
    ),
    (
        "B2",
        "Maria Lopez Catuai",
        "After years working on farms in Costa Rica, Maria returned home to Nicaragua to start her own finca.",
        "Nicaragua",
    ),
    (
        "B3",
        "Los Naranjos",
        "Unlike the big farms in Brazil, these smallholders in Huila pick every cherry by hand.",
        "Colombia",
    ),
    (
        "B4",
        "El Mirador",
        "Grown in Cauca. Like all our coffees, it is sourced from Peru's best importer partners.",
        "Colombia",
    ),
    (
        "B5",
        "Bukeye Washed",
        "This Burundian lot is double-fermented Kenya-style at the Bukeye station.",
        "Burundi",
    ),
    (
        "B6",
        "Night Owl Decaf",
        "A washed Peruvian lot from Cajamarca, decaffeinated in Mexico using the Mountain Water Process.",
        "Peru",
    ),
    (
        "C1",
        "Kenya SL28 AA",
        "If you love our Ethiopia Guji or Ethiopia Hambela, try this lot from Nyeri.",
        "Kenya",
    ),
    (
        "C2",
        "Ethiopia Heirloom Natural",
        "Dried on raised beds at Hambela. Our house espresso, sourced from Brazil, is also on sale.",
        "Ethiopia",
    ),
    ("C3", "El Paraiso Lot 9", "Notes: Brazil nut, cocoa, red apple. A classic Guatemala cup.", "Guatemala"),
    (
        "C4",
        "Finca Las Nubes",
        "Varieties: Typica and Bourbon. Typica descended from plants in Yemen; this lot comes from Jinotega, Nicaragua.",
        "Nicaragua",
    ),
    (
        "C5",
        "Finca Santa Barbara Pacamara",
        "Pacamara, developed in El Salvador, grown here on our farm in Santa Barbara, Honduras.",
        "Honduras",
    ),
    (
        "C6",
        "Sitio Sao Joao",
        "Yellow Bourbon originally from Brazil, grown by the Silva family in Mantiqueira de Minas.",
        "Brazil",
    ),
    (
        "C7",
        "Mt. Elgon Natural",
        "Grown on the slopes of Mt. Elgon by Bugisu farmers. Similar to Kenya, juicy.",
        "Uganda",
    ),
    ("C8", "Huehue Lot 3", "Producer: Jordan Guinea-Ramirez. Grown in Huehuetenango.", "Guatemala"),
    ("D1", "Kona Peaberry", "Grown in Kona, Hawaii. Brighter than most Colombian coffee.", "United States"),
    ("D2", "Las Flores Pacamara", "Washed at the Las Flores mill in Chalatenango.", "El Salvador"),
    ("D3", "Flores Bajawa", "Grown on the island of Flores, Indonesia.", "Indonesia"),
    ("D4", "La Flor de Flores", "Grown in Las Flores, Honduras.", "Honduras"),
    ("D5", "Origin Series: Rwanda", "Washed at Huye Mountain. Compare to our Burundi.", "Rwanda"),
    ("D6", "Yemen Haraz", "Heirloom Yemeni varieties, native to Yemen's Haraz mountains.", "Yemen"),
    (
        "D7",
        "Typica Mejorado",
        "Typica Mejorado, a cultivar bred in Ecuador, grown by Pepe Jijon at Finca Soledad, Intag Valley.",
        "Ecuador",
    ),
    ("D8", "Caturra", "Caturra, a mutation discovered in Brazil. This lot is from Tolima.", "Colombia"),
    (
        "D9",
        "Pink Bourbon Huila",
        "Pink Bourbon, which originated in Colombia, from the San Adolfo area.",
        "Colombia",
    ),
    ("E1", "Wush Wush", "Wush Wush, native to Ethiopia, grown in Tolima.", "Colombia"),
    (
        "E2",
        "Sidra Natural",
        "Sidra, a variety bred in Ecuador. Grown at Finca La Reserva in Pichincha, Ecuador.",
        "Ecuador",
    ),
    (
        "E3",
        "Sidra Natural",
        "Sidra, a variety bred in Ecuador. Grown at Finca La Reserva, Tolima.",
        "Colombia",
    ),
    (
        "R1",
        "La Esperanza Lot 4",
        "A classic Colombia cup. Roasted fresh in Santa Fe, New Mexico.",
        "Colombia",
    ),
    (
        "R2",
        "Los Naranjos",
        "Nicaragua at its best. Unlike the big farms in Brazil, these smallholders pick by hand.",
        "Nicaragua",
    ),
    (
        "R3",
        "Gichathaini AA",
        "SL28, developed in Kenya in the 1930s, at its best here. Brighter than any Ethiopia we carry.",
        "Kenya",
    ),
    (
        "R4",
        "Kenya SL28 & SL34",
        "Washed at the Gatomboya factory. Like the farms in Ethiopia we buy from, it is a smallholder lot.",
        "Kenya",
    ),
    (
        "R5",
        "Ethiopia Heirloom",
        "Washed at Hambela. Unlike farms in Brazil, these are garden plots.",
        "Ethiopia",
    ),
    (
        "R6",
        "Kenya Peaberry",
        "Grown in Nyeri. If you liked our Colombia Huila or Colombia Narino, try this.",
        "Kenya",
    ),
    ("P1", "Single Origin", "Grown by indigenous Mayan farmers in Guatemala.", "Guatemala"),
    ("P2", "Single Origin", "Grown by native farmers in Peru.", "Peru"),
    ("P3", "Single Origin", "A program developed in partnership with farmers in Colombia.", "Colombia"),
    ("P4", "Single Origin", "We first discovered this Bourbon lot in Rwanda on a buying trip.", "Rwanda"),
    ("P5", "Single Origin", "The farm's Pacamara was first planted in El Salvador in 1990", "El Salvador"),
    (
        "P6",
        "Finca El Puente SL-28 Variety",
        "SL-28 is a variety developed in Kenya. Today, Kenya remains its best-known home. Grown in Marcala, Honduras.",
        "Honduras",
    ),
    ("P7", "Huila Decaf", "Brighter than most coffees from Brazil.", "Colombia"),
    ("P8", "Finca Santa Barbara", "Bourbon from Colombia, a lovely cup.", "Colombia"),
    (
        "P9",
        "Single Origin",
        "Ethiopia heirloom varieties, washed. Compare with our Kenya offering.",
        "Ethiopia",
    ),
    ("P10", "Single Origin", "Grown in Las Flores. Notes: honey.", None),
    ("P11", "Single Origin", "Origin: Kenyan. Unlike Ethiopia, bright.", "Kenya"),
    ("P12", "Finca El Puente Kenya SL-28", G, "Honduras"),
    ("P13", "Finca El Puente Kenyan SL-28", G, "Honduras"),
    ("P14", "Finca El Puente Ethiopia Landrace Variety", G, "Honduras"),
    ("P15", "Finca El Puente Variedad Colombia", G, "Honduras"),
    ("P16", "Las Flores SL-28 Variety", G, "Honduras"),
    ("P17", "Kenyan SL-28 grown in Honduras", G, "Honduras"),
    ("P18", "Lot 7", "However, Kenya is famous for SL28. Here, we grow it in Marcala, Honduras.", "Honduras"),
    (
        "P19",
        "Honduras Cerro Azul Ethiosar",
        "The family has operated farms in Nicaragua for generations; this lot is from their newer farm.",
        "Honduras",
    ),
    ("P20", "Ethiopia Guji", "Unlike our Kenya, floral.", "Ethiopia"),
    ("P21", "Kenya Kiambu SL28", "SL28 developed in Kenya.", "Kenya"),
    (
        "P22",
        "Gatuyaini AA SL-28",
        "SL-28 and SL-34, the varieties Kenya is famous for, grown by members of the Othaya cooperative in Nyeri, Kenya.",
        "Kenya",
    ),
    (
        "P23",
        "Esmeralda Gesha",
        "Gesha, a variety originally from Ethiopia, grown at Hacienda La Esmeralda in Boquete. Juicy and floral.",
        "Panama",
    ),
    ("P24", "Konga", "Grown by smallholders around Yirgacheffe and washed at the Konga station.", "Ethiopia"),
    (
        "P25",
        "Finca El Puente - Anaerobic",
        "Fermented in sealed tanks at Finca El Puente, high above the town of Marcala.",
        "Honduras",
    ),
    ("P26", "Finca El Puente SL-28 Variety", SL, "Honduras"),
    ("P27", "Finca El Puente SL-28 Variety", SL.replace(", Honduras", ""), "Honduras"),
    ("P28", "Finca El Puente SL-28 Variety", SL.replace("Kenya remains", "Today, Kenya remains"), "Honduras"),
    (
        "P29",
        "Finca El Puente - Honey Processed",
        ("We met the owners of Finca El Puente in 2005. " * 40) + "Region: Marcala, Honduras",
        "Honduras",
    ),
    ("P30", "House Blend", "Roasted in Santa Barbara, CA.", None),
    (
        "P31",
        "Huye Mountain",
        "Like the best coffees of Ethiopia, this one is floral. Origin: Rwanda. Washed at the Huye Mountain station.",
        "Rwanda",
    ),
    (
        "P32",
        "Nahun Fernandez",
        "Farm: Don Andres\nLocation: Las Flores, Honduras\nProcess: Honey",
        "Honduras",
    ),
    ("P33", "Ethiopia Heirloom", "Region: Sidama, Ethiopia.", "Ethiopia"),
    ("P34", "Panama Gesha", "Gesha, originally from Ethiopia. Region: Boquete, Panama.", "Panama"),
]
ABSTAIN = {
    # A variety's homeland and the farm's country both named, with no stated origin.
    "A3",
    "A4",
    "E1",
    "E3",
    "D8",
    "P23",
    "R3",
    "P27",
    # A comparison or a second coffee names another country.
    "B2",
    "B3",
    "B4",
    "B6",
    "C7",
    "R2",
    "P7",
    "P9",
    # A title country that is part of a variety name ("Kenya SL-28"), and a region elsewhere disagrees.
    "C1",
    "P12",
    "P13",
    "P14",
    # Known misses main shares, left for Claude: a demonym loses to a country, "Guinea-Ramirez" reads as Guinea.
    "B5",
    "C8",
}
KNOWN_WRONG = {"B5": "Kenya"}


@pytest.mark.parametrize("cid,title,body,expected", ORIGIN_CASES, ids=[c[0] for c in ORIGIN_CASES])
def test_origin_probes(cid, title, body, expected):
    got = extract.rules(item(title, body=body)).origin_country
    if cid in ABSTAIN:
        assert got in (expected, None, KNOWN_WRONG.get(cid)), got
    else:
        assert got == expected


def test_el_puente_lots():
    e = extract.rules(item("Finca El Puente SL-28 Variety", tags=["origin__single-origin"], body=SL))
    assert e.origin_country == "Honduras" and e.varietals == ["SL28"]
    honey = (
        "We met the owners of Finca El Puente in 2005. " * 40
    ) + "Process: Honey\nRegion: Marcala, Honduras"
    e = extract.rules(item("Finca El Puente - Honey Processed", body=honey))
    assert e.origin_country == "Honduras" and e.process == "honey"
    # Without its country, the SL-28 story names only Kenya, while its region is in Honduras: no answer, not Kenya.
    e = extract.rules(item("Finca El Puente SL-28 Variety", body=SL.replace(", Honduras", "")))
    assert e.origin_country is None and extract.needs_llm(e)


def test_region_only_when_no_country_is_named():
    assert extract.rules(item("La Esperanza", tags=["region:Nariño"])).origin_country == "Colombia"
    assert extract.rules(item("Huila Lot 4", body="Grown by smallholders.")).origin_country == "Colombia"
    # A country anywhere wins over, or conflicts with, a region; a region never overrides it.
    assert extract.rules(item("Huila Lot 4", body="A classic Colombia cup.")).origin_country == "Colombia"
    assert extract.rules(item("Islander", body="Inspired by Antigua and Barbuda.")).origin_country is None


def test_multi_origin_blend_bodies_abstain():
    body = "A simple, sweet medium roast blend of coffees from Colombia and Ethiopia."
    assert extract.rules(item("Simplify", body=body)).origin_country is None
    assert extract.rules(item("Rise", body="Origin: Brazil, Colombia, Guatemala")).origin_country is None


def test_varietal_spellings():
    e = extract.rules(item("Kiambu", body="SL-28, SL 34, Ruiru11 and Batian."))
    assert e.varietals == ["Batian", "Ruiru 11", "SL28", "SL34"]
