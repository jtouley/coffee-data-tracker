{#- HTML character references in store text, decoded in the build (ingestion keeps the raw value). DuckDB has no
    built-in for this. Some stores' APIs escape text (WooCommerce's Store API returns term names escaped: "Parts
    &amp; Equipment"), and some escape it twice ("Education &amp;amp; Display"), so decode_html_entities runs up to
    `passes` rounds, as CatalogItem does for titles (models.py).

    One round decodes every reference that ends in ';', as Python's html.unescape does: decimal (&#8211;) and hex
    (&#x2013;) numbers with its replacements (0, a surrogate or past U+10FFFF: U+FFFD; 0x80-0x9F: windows-1252,
    so &#150; is an en dash; other control and non-characters: dropped), and the named ones in html_entity_names().
    Unlike html.unescape, an unknown name and a reference without its ';' ("R&D", "&amp") are left as they are.
    tests/test_html_entities.py checks a round against html.unescape.

    Names are looked up case-sensitively, as HTML defines them: case changes the character (&Ecirc; is Ê, &ecirc;
    is ê, &Gt; is ≫), so lowercasing a name would decode some wrongly. The uppercase spellings HTML accepts for the
    same character (&AMP;, &LT;, &GT;, &QUOT;, &COPY;, &REG;, &TRADE;) are in the map as names of their own.

    A tag with no '&' has nothing to decode and is kept as it is. For a whole table (stg_snapshots,
    int_fetch_product_inputs), html_tag_decodings decodes each distinct tag that has one once, and
    decode_tags_with looks tags up in the result, so the build pays for the few escaped tags, not for every row:
    decoding row by row, even skipping tags with no '&', took about 1s more per table on the lake of 2026-11-02
    (274k snapshot rows, 2.5k distinct escaped tags). -#}

{% macro html_entity_names() -%}
map {
    'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'apos': '''', 'nbsp': chr(160), 'shy': chr(173),
    'ensp': chr(8194), 'emsp': chr(8195), 'thinsp': chr(8201), 'zwnj': chr(8204), 'zwj': chr(8205),
    'ndash': chr(8211), 'mdash': chr(8212), 'lsquo': chr(8216), 'rsquo': chr(8217), 'sbquo': chr(8218),
    'ldquo': chr(8220), 'rdquo': chr(8221), 'bdquo': chr(8222), 'laquo': chr(171), 'raquo': chr(187),
    'lsaquo': chr(8249), 'rsaquo': chr(8250), 'hellip': chr(8230), 'bull': chr(8226), 'middot': chr(183),
    'deg': chr(176), 'copy': chr(169), 'reg': chr(174), 'trade': chr(8482), 'times': chr(215), 'divide': chr(247),
    'frac14': chr(188), 'frac12': chr(189), 'frac34': chr(190), 'cent': chr(162), 'pound': chr(163),
    'euro': chr(8364), 'yen': chr(165), 'sect': chr(167), 'para': chr(182), 'iexcl': chr(161), 'iquest': chr(191),
    'aacute': chr(225), 'Aacute': chr(193), 'agrave': chr(224), 'Agrave': chr(192), 'acirc': chr(226),
    'atilde': chr(227), 'auml': chr(228), 'Auml': chr(196), 'aring': chr(229), 'aelig': chr(230), 'ccedil': chr(231),
    'Ccedil': chr(199), 'eacute': chr(233), 'Eacute': chr(201), 'egrave': chr(232), 'Egrave': chr(200),
    'ecirc': chr(234), 'euml': chr(235), 'iacute': chr(237), 'Iacute': chr(205), 'igrave': chr(236),
    'icirc': chr(238), 'iuml': chr(239), 'ntilde': chr(241), 'Ntilde': chr(209), 'oacute': chr(243),
    'Oacute': chr(211), 'ograve': chr(242), 'ocirc': chr(244), 'otilde': chr(245), 'ouml': chr(246),
    'Ouml': chr(214), 'oslash': chr(248), 'uacute': chr(250), 'Uacute': chr(218), 'ugrave': chr(249),
    'ucirc': chr(251), 'uuml': chr(252), 'Uuml': chr(220), 'yacute': chr(253), 'szlig': chr(223),
    'Acirc': chr(194), 'Atilde': chr(195), 'Aring': chr(197), 'AElig': chr(198), 'Ecirc': chr(202),
    'Euml': chr(203), 'Igrave': chr(204), 'Icirc': chr(206), 'Iuml': chr(207), 'Ograve': chr(210),
    'Ocirc': chr(212), 'Otilde': chr(213), 'Oslash': chr(216), 'Ugrave': chr(217), 'Ucirc': chr(219),
    'Yacute': chr(221), 'Lt': chr(8810), 'Gt': chr(8811),
    'AMP': '&', 'LT': '<', 'GT': '>', 'QUOT': '"', 'COPY': chr(169), 'REG': chr(174), 'TRADE': chr(8482)
}
{%- endmacro %}

{#- One round over a VARCHAR expression (null stays null). Splits the text into references and the text between
    them, replaces each reference, and joins the pieces. -#}
{% macro decode_html_entities_once(text) -%}
array_to_string(list_transform(
    regexp_extract_all({{ text }}, '&#[0-9]+;|&#[xX][0-9a-fA-F]+;|&[A-Za-z][A-Za-z0-9]*;|[^&]+|&'),
    lambda tok: case
        when not (tok ^@ '&' and tok[-1] = ';') then tok
        when not (tok ^@ '&#') then coalesce({{ html_entity_names() }}[tok[2:-2]], tok)
        -- a number: its digits without leading zeros, at most 7 (more is past U+10FFFF), as a code point
        else list_transform([list_transform([ltrim(if(lower(tok[3]) = 'x', tok[4:-2], tok[3:-2]), '0')], lambda d:
                if(length(d) > 7, 1114112, if(d = '', 0,
                   if(lower(tok[3]) = 'x', ('0x' || d)::bigint, d::bigint))))[1]], lambda cp: case
            when cp = 0 or cp between 55296 and 57343 or cp > 1114111 then chr(65533)
            when cp between 128 and 159 then chr([8364, 129, 8218, 402, 8222, 8230, 8224, 8225, 710, 8240, 352, 8249,
                338, 141, 381, 143, 144, 8216, 8217, 8220, 8221, 8226, 8211, 8212, 732, 8482, 353, 8250, 339, 157,
                382, 376][cp - 127])
            when cp between 1 and 8 or cp = 11 or cp between 14 and 31 or cp = 127
                or cp between 64976 and 65007 or cp % 65536 >= 65534 then ''
            else chr(cp::integer) end)[1]
    end), '')
{%- endmacro %}

{#- Exactly `passes` rounds (a text escaped more times than that keeps the rest: with 3, '&amp;amp;amp;amp;' gives
    '&amp;'). -#}
{% macro decode_html_entities(text, passes=3) -%}
{%- if passes > 0 -%}
{{ decode_html_entities(decode_html_entities_once(text), passes - 1) }}
{%- else -%}
{{ text }}
{%- endif -%}
{%- endmacro %}

{#- Each element of a VARCHAR[] (null stays null). An element with no '&' is kept as it is, without decoding. -#}
{% macro decode_html_entities_list(items, passes=3) -%}
list_transform({{ items }}, lambda html_item: if(contains(html_item, '&'),
    {{ decode_html_entities('html_item', passes) }}, html_item))
{%- endmacro %}


{#- A one-row relation with tag_map: every distinct tag of `items` (a VARCHAR[] column of `relation`) that holds a
    '&', mapped to its decoded form (null when no tag has one). Cross join it and decode with decode_tags_with. -#}
{% macro html_tag_decodings(relation, items='tags') -%}
(select map_from_entries(list({'k': html_tag, 'v': {{ decode_html_entities('html_tag') }}})) as tag_map
 from (select distinct unnest({{ items }}) as html_tag from {{ relation }})
 where contains(html_tag, '&'))
{%- endmacro %}

{#- Each element of a VARCHAR[] through a tag_map from html_tag_decodings: the same result as
    decode_html_entities_list, for tags the map was built from (null stays null). -#}
{% macro decode_tags_with(items, tag_map) -%}
list_transform({{ items }}, lambda html_item: coalesce({{ tag_map }}[html_item], html_item))
{%- endmacro %}
