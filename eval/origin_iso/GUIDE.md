# Labeling guide: origin spelling to ISO 3166-1

You get a list of items. Each is one **spelling**: a value the pipeline recorded as a coffee's origin country, exactly
as recorded (capitals, accents and punctuation included), with up to five example listings that carry it (title,
roaster, and the origin region where one was recorded). Give each item one label saying what the spelling denotes
as a coffee's origin.

## Labels

Use exactly one of these:

| Label | When |
|---|---|
| an ISO 3166-1 **alpha-2 code**, e.g. `BR` | The spelling names exactly one country. |
| `MULTIPLE` | It names two or more countries, or places in two or more countries, or a country together with a region larger than a country. |
| `NOT_A_COUNTRY` | A continent or a region larger than one country ("Asia", "the Caribbean"), or not a place at all. |
| `AMBIGUOUS` | One place, but the spelling alone can't say which country it is in (a place two countries share, a name two countries use). |

## How to decide

1. **Judge the spelling.** Ask: if a listing said "Origin: *spelling*", which country would that be? The examples
   are context for reading the spelling (a misspelling, another language, an abbreviation); they don't change what
   the spelling means. If the examples suggest the value was recorded wrongly for those listings (the titles name a
   different country), still label the spelling, and say so in the note.
2. **One country, however written:** other languages, accents and misspellings name the country they mean
   ("Etiopía" `ET`, "Kenia" `KE`). Old or common names count ("Burma" `MM`, "Swaziland" `SZ`).
3. **A place inside one country is that country:** a state, island, province or growing region ("Yunnan" `CN`,
   "Java" `ID`). A place named with its country is still that one country ("Yunnan, China" `CN`).
4. **ISO lists some territories as their own entries.** Use the territory's own code where ISO 3166-1 gives it one
   (for example Guadeloupe, Réunion, Guam), not the code of the country that administers it.
5. **Lists:** two or more countries in any form ("A, B", "A & B", "A/B", "A and B", "A (B)" where B is another
   country) are `MULTIPLE`. A country listed with a continent or wide region is `MULTIPLE` too. A country whose own
   name contains "and" ("Trinidad and Tobago") is one country.
6. **Regions larger than a country** are `NOT_A_COUNTRY`, even when most coffee from there comes from one country.
7. If you are unsure between two labels, pick the one you think more likely and say why in the note.

Look up codes in the ISO 3166-1 table given with the packet (`alpha2` column; the `iso_short_name` and
`common_name` columns help find a country). Don't use any other file or source.

## Output

Write one JSON object to the file you are told, keyed by item id, every item labeled:

```json
{"S001": {"label": "MULTIPLE", "note": ""},
 "S002": {"label": "ET", "note": "Spanish spelling"}}
```

`label` is a code in upper case or one of the three words above. `note` is optional, one short line.
