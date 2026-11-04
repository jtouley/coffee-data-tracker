"""dbt's decode_html_entities macro (dbt/macros/html_entities.sql), rendered and run in DuckDB, against Python's
html.unescape: every numeric reference, every name in its map, and the text the real lake has."""

import html
import html.entities
import re
from pathlib import Path

import duckdb
import pytest

jinja2 = pytest.importorskip("jinja2")

MACROS = (Path(__file__).parent.parent / "dbt" / "macros" / "html_entities.sql").read_text()


def render(call: str) -> str:
    return (
        jinja2.Environment(extensions=["jinja2.ext.do"]).from_string(MACROS + "{{ " + call + " }}").render()
    )


@pytest.fixture(scope="module")
def con():
    return duckdb.connect()


def decode(con, texts: list[str | None], passes: int = 1) -> list[str | None]:
    expr = render(f"decode_html_entities('x', {passes})")
    sql = f"select {expr} from unnest(?::varchar[]) with ordinality t(x, i) order by i"
    return [r[0] for r in con.execute(sql, [texts]).fetchall()]


def test_every_numeric_reference_decodes_as_html_unescape_does(con):
    """Decimal and hex, every code point and past the last one, with Python's replacements: U+FFFD for 0, a
    surrogate or past U+10FFFF, windows-1252 for 0x80-0x9F, nothing for other control and non-characters."""
    cps = list(range(0x110000)) + [0x110000, 0x7FFFFFFF, 10**30]
    refs = [f"&#{n};" for n in cps] + [f"&#x{n:X};" for n in cps] + [f"&#x{n:06x};" for n in cps[:300]]
    assert decode(con, refs) == [html.unescape(r) for r in refs]


def test_every_named_reference_in_the_map_decodes_as_html_unescape_does(con):
    names = re.findall(r"'(\w+)': ", render("html_entity_names()"))
    assert len(names) == len(set(names)) == 110
    refs = [f"&{n};" for n in names]
    assert (
        decode(con, refs) == [html.entities.html5[f"{n};"] for n in names] == [html.unescape(r) for r in refs]
    )


@pytest.mark.parametrize(
    "raw, want",
    [
        # As the lake has them (WooCommerce term names; a Shopify store's tags escaped twice).
        ("Parts &amp; Equipment", "Parts & Equipment"),
        ("Coffee &gt; Blend &gt; Espresso Blend", "Coffee > Blend > Espresso Blend"),
        ("Education &amp;amp; Display", "Education & Display"),
        ("Light &#8211; Medium", "Light – Medium"),
        ("Caf&eacute; &#X2013; Bar", "Café – Bar"),
        # Left as they are: a bare '&', an unknown name, a reference without its ';' (html.unescape decodes it).
        ("R&D && more &", "R&D && more &"),
        ("&bogus; &amp", "&bogus; &amp"),
        ("", ""),
        (None, None),
        # Exactly three rounds, like titles (models.CatalogItem): a fourth level of escaping keeps one.
        ("&amp;amp;amp;amp;", "&amp;"),
        # Case: the uppercase names HTML accepts decode; others are different characters or unknown.
        ("&AMP; &LT;b&GT; &QUOT;&COPY;&REG;&TRADE;", '& <b> "©®™'),
        ("&Ecirc; &ecirc; &Gt; &gt;", "Ê ê ≫ >"),
        ("&Amp; &aMP;", "&Amp; &aMP;"),
    ],
)
def test_text_decodes_in_up_to_three_rounds(con, raw, want):
    assert decode(con, [raw], passes=3) == [want]


def test_case_follows_html_unescape():
    """The macro's names are case-sensitive as HTML's are: each case variant either decodes as html.unescape does
    or is one html.unescape doesn't know either."""
    names = set(re.findall(r"'(\w+)': ", render("html_entity_names()")))
    for n in names:
        for v in {n.upper(), n.lower(), n.capitalize()} - names:
            assert (
                f"{v};" not in html.entities.html5
                or html.entities.html5[f"{v};"] != html.entities.html5[f"{n};"]
            )


def test_a_list_decodes_each_tag_and_keeps_nulls(con):
    expr = render("decode_html_entities_list('t')")
    sql = f"select {expr} from (select ?::varchar[] as t)"
    assert con.execute(sql, [["A &amp; B", None, "C"]]).fetchone()[0] == ["A & B", None, "C"]
    assert con.execute(sql, [None]).fetchone()[0] is None
    # A tag with no '&' is kept as it is (not decoded at all).
    assert con.execute(sql, [["Kenya", "", "100% Arabica"]]).fetchone()[0] == ["Kenya", "", "100% Arabica"]


@pytest.mark.parametrize(
    "rows",
    [
        [["A &amp; B", None, "C"], None, [], ["Education &amp;amp; Display", "A &amp; B", "R&D", "&AMP;"]],
        [["Kenya"], None, []],  # no tag has a '&': the map is null and every tag is kept
    ],
)
def test_decoding_a_table_through_its_tag_map_matches_decoding_each_row(con, rows):
    """stg_snapshots and int_fetch_product_inputs decode each distinct escaped tag once (html_tag_decodings) and
    look tags up (decode_tags_with): the same tags as decoding every row."""
    con.execute(
        "create or replace temp table snaps as select * from unnest(?::varchar[][]) with ordinality t(tags, i)",
        [rows],
    )
    by_map = render("decode_tags_with('s.tags', 'html.tag_map')")
    decodings = render("html_tag_decodings('snaps')")
    by_row = render("decode_html_entities_list('s.tags')")
    got = con.execute(f"select {by_map} from snaps s cross join {decodings} html order by i").fetchall()
    want = con.execute(f"select {by_row} from snaps s order by i").fetchall()
    assert got == want
