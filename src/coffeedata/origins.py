"""Coffee-producing countries (seeded from the original tracker form) and common aliases."""

COUNTRIES = [
    "Bolivia", "Brazil", "Burundi", "Cameroon", "Central African Republic", "China", "Colombia",
    "Costa Rica", "Cuba", "Democratic Republic of the Congo", "DR Congo", "Congo", "Dominican Republic",
    "Ecuador", "El Salvador", "Ethiopia", "Gabon", "Guatemala", "Guinea", "Haiti", "Hawaii", "Kona",
    "Honduras", "India", "Indonesia", "Sumatra", "Java", "Sulawesi", "Bali", "Flores", "Ivory Coast",
    "Jamaica", "Kenya", "Laos", "Madagascar", "Malawi", "Malaysia", "Mexico", "Myanmar", "Nepal",
    "Nicaragua", "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines", "Puerto Rico", "Rwanda",
    "Sierra Leone", "Sri Lanka", "Tanzania", "Thailand", "Timor-Leste", "East Timor", "Trinidad and Tobago",
    "Uganda", "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
]

ALIASES = {
    "sumatra": "Indonesia", "java": "Indonesia", "sulawesi": "Indonesia", "bali": "Indonesia",
    "flores": "Indonesia", "kona": "United States", "hawaii": "United States", "puerto rico": "United States",
    "dr congo": "Democratic Republic of the Congo", "congo": "Democratic Republic of the Congo",
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
