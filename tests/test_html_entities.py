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
    assert len(names) == len(set(names)) == 85
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
        # Three rounds, like titles (models.CatalogItem): a fourth level of escaping keeps one.
        ("&amp;amp;amp;amp;", "&amp;"),
    ],
)
def test_text_decodes_until_it_stops_changing(con, raw, want):
    assert decode(con, [raw], passes=3) == [want]


def test_a_list_decodes_each_tag_and_keeps_nulls(con):
    expr = render("decode_html_entities_list('t')")
    sql = f"select {expr} from (select ?::varchar[] as t)"
    assert con.execute(sql, [["A &amp; B", None, "C"]]).fetchone()[0] == ["A & B", None, "C"]
    assert con.execute(sql, [None]).fetchone()[0] is None
