"""Coffee-producing countries (seeded from the original tracker form) and common aliases."""

COUNTRIES = [
    "Bolivia",
    "Brazil",
    "Burundi",
    "Cameroon",
    "Central African Republic",
    "China",
    "Colombia",
    "Costa Rica",
    "Cuba",
    "Democratic Republic of the Congo",
    "DR Congo",
    "Congo",
    "Dominican Republic",
    "Ecuador",
    "El Salvador",
    "Ethiopia",
    "Gabon",
    "Guatemala",
    "Guinea",
    "Haiti",
    "Hawaii",
    "Kona",
    "Honduras",
    "India",
    "Indonesia",
    "Sumatra",
    "Java",
    "Sulawesi",
    "Bali",
    "Flores",
    "Ivory Coast",
    "Jamaica",
    "Kenya",
    "Laos",
    "Madagascar",
    "Malawi",
    "Malaysia",
    "Mexico",
    "Myanmar",
    "Nepal",
    "Nicaragua",
    "Panama",
    "Papua New Guinea",
    "Paraguay",
    "Peru",
    "Philippines",
    "Puerto Rico",
    "Rwanda",
    "Sierra Leone",
    "Sri Lanka",
    "Tanzania",
    "Thailand",
    "Timor-Leste",
    "East Timor",
    "Trinidad and Tobago",
    "Uganda",
    "Venezuela",
    "Vietnam",
    "Yemen",
    "Zambia",
    "Zimbabwe",
]

ALIASES = {
    "sumatra": "Indonesia",
    "java": "Indonesia",
    "sulawesi": "Indonesia",
    "bali": "Indonesia",
    "flores": "Indonesia",
    "kona": "United States",
    "hawaii": "United States",
    "puerto rico": "United States",
    "dr congo": "Democratic Republic of the Congo",
    "congo": "Democratic Republic of the Congo",
    "east timor": "Timor-Leste",
}


def canonical_country(name: str) -> str:
    key = name.strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    for c in COUNTRIES:
        if c.lower() == key:
            return c
    return name.strip().title()


# Coffee-growing regions that belong to exactly one country, for listings that name the region but not the country
# ("grown in Marcala"). Only names that mean nothing else in a coffee listing: no regions shared across a border
# (Mount Elgon, Rwenzori, Yungas, Meru), no names that are also common elsewhere (Santa Ana, San Marcos, La Paz,
# Volcán, Amazonas, Jaén, Loja, Santander), and nothing in COUNTRIES or ALIASES. "Antigua" means Guatemala here;
# extract.py drops "Antigua and Barbuda", and Santa Barbara followed by California, before looking these up.
REGIONS = {
    # Honduras
    "marcala": "Honduras",
    "santa barbara": "Honduras",
    "santa bárbara": "Honduras",
    "copan": "Honduras",
    "copán": "Honduras",
    "ocotepeque": "Honduras",
    "intibuca": "Honduras",
    "intibucá": "Honduras",
    "comayagua": "Honduras",
    # Colombia
    "huila": "Colombia",
    "nariño": "Colombia",
    "narino": "Colombia",
    "cauca": "Colombia",
    "tolima": "Colombia",
    "quindio": "Colombia",
    "quindío": "Colombia",
    "risaralda": "Colombia",
    "antioquia": "Colombia",
    # Ethiopia
    "yirgacheffe": "Ethiopia",
    "yirgachefe": "Ethiopia",
    "yirga cheffe": "Ethiopia",
    "guji": "Ethiopia",
    "sidama": "Ethiopia",
    "sidamo": "Ethiopia",
    "gedeo": "Ethiopia",
    "gedeb": "Ethiopia",
    "kochere": "Ethiopia",
    "hambela": "Ethiopia",
    "shakiso": "Ethiopia",
    "limu": "Ethiopia",
    "jimma": "Ethiopia",
    "harrar": "Ethiopia",
    "harar": "Ethiopia",
    "bench maji": "Ethiopia",
    # Kenya
    "nyeri": "Kenya",
    "kirinyaga": "Kenya",
    "murang'a": "Kenya",
    "muranga": "Kenya",
    "kiambu": "Kenya",
    "embu": "Kenya",
    "othaya": "Kenya",
    # Guatemala
    "huehuetenango": "Guatemala",
    "antigua": "Guatemala",
    "acatenango": "Guatemala",
    "atitlan": "Guatemala",
    "atitlán": "Guatemala",
    "fraijanes": "Guatemala",
    "coban": "Guatemala",
    "cobán": "Guatemala",
    # Costa Rica
    "tarrazu": "Costa Rica",
    "tarrazú": "Costa Rica",
    # El Salvador
    "apaneca": "El Salvador",
    "chalatenango": "El Salvador",
    # Panama
    "boquete": "Panama",
    "chiriqui": "Panama",
    "chiriquí": "Panama",
    # Nicaragua
    "jinotega": "Nicaragua",
    "matagalpa": "Nicaragua",
    "nueva segovia": "Nicaragua",
    # Mexico
    "chiapas": "Mexico",
    "oaxaca": "Mexico",
    "veracruz": "Mexico",
    # Peru
    "cajamarca": "Peru",
    # Bolivia
    "caranavi": "Bolivia",
    # Brazil
    "minas gerais": "Brazil",
    "sul de minas": "Brazil",
    "cerrado mineiro": "Brazil",
    "mogiana": "Brazil",
    "mantiqueira": "Brazil",
    # Rwanda
    "nyamasheke": "Rwanda",
    "huye": "Rwanda",
    # Burundi
    "kayanza": "Burundi",
    "ngozi": "Burundi",
    # Uganda
    "bugisu": "Uganda",
    # Tanzania
    "mbeya": "Tanzania",
    # Indonesia
    "aceh": "Indonesia",
    "gayo": "Indonesia",
    "toraja": "Indonesia",
    # Yemen
    "haraz": "Yemen",
}
