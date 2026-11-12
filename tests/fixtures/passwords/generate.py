"""Passwords in the shapes password managers make, for tests/test_rules.py (spec §12). Seeded, so the output is
the same each run: `python tests/fixtures/passwords/generate.py > tests/fixtures/passwords/passwords.json`.

1Password's generator, as read from its web generator's code (spec §2):
- Random: 8 to 100 characters from letters without the look-alikes 0O1Il5S, digits as a toggle, and symbols as a
  toggle, from exactly `!#%)*+,-.:=>?@]^_}~`.
- Memorable: 3 to 15 lower-case words of 3 to 8 letters, optionally capitalised, joined by a separator: hyphens on
  the web; digits, spaces, underscores, periods and none in the library; commas and "numbers and symbols" in the
  apps. WORDS below is a 200-word stand-in of the same shape, not 1Password's own list.
Then Safari's and Chrome's suggestions, a passphrase with spaces, and emoji.
"""

import json
import random
import string

LETTERS = "ABCDEFGHJKLMNPQRTUVWXYZabcdefghijkmnopqrstuvwxyz"
DIGITS = "2346789"
SYMBOLS = "!#%)*+,-.:=>?@]^_}~"
WORDS = """
able acid aged also arch area army atom aunt away axis baby back bake ball band bank barn base bath bead beam
bean bear beat bell belt bend best bike bird bite blue boat body bold bolt bond bone book boot born boss bowl
brew brick bridge bright broad brook brush bulb burst cabin cable camel candle canoe canyon carbon cargo carpet
castle cedar cellar chalk charm cherry chess chief cider circle citrus clay cliff cloud clover cobalt cocoa
comet copper coral cotton cove crane crater crisp crown cubic dawn delta desert dial dock dome dune eagle
early earth echo elbow ember empty engine fable falcon fern ferry fiber field flint flora focus forest fossil
frost garnet gentle ginger glacier globe gold grain granite gravel harbor hazel hollow honey island ivory
jacket jasper jungle kettle lagoon lantern lemon linen lunar maple marble meadow melon mint mosaic nectar
noble north oasis olive onyx orbit orchid otter oyster paddle pebble pepper pine planet plum pocket polar
quarry quartz quiet radar raven ridge river robin saddle salt sand satin scarf shadow silver sketch slate
""".split()  # noqa: SIM905 - easier to read as text
SEPARATORS = {"hyphen": "-", "space": " ", "period": ".", "comma": ",", "underscore": "_", "digit": DIGITS, "digit_or_symbol": DIGITS + SYMBOLS, "none": ""}  # fmt: skip

rng = random.Random(20261106)
out = []
lengths = (8, 9, 10, 20, 64, 100)
toggles = ((False, False), (True, False), (False, True), (True, True))
for i in range(60):
    n, (digits, symbols) = lengths[i % 6], toggles[(i // 6) % 4]
    pool = LETTERS + (DIGITS if digits else "") + (SYMBOLS if symbols else "")
    out.append(
        {
            "kind": "random",
            "digits": digits,
            "symbols": symbols,
            "password": "".join(rng.choice(pool) for _ in range(n)),
        }
    )
names = list(SEPARATORS)
for i in range(60):
    words, sep, cap = (3, 4, 15)[i % 3], names[(i // 3) % len(names)], (i // 24) % 2 == 1
    picked = [rng.choice(WORDS) for _ in range(words)]
    if cap:
        picked = [w.capitalize() for w in picked]
    chars = SEPARATORS[sep]
    pw = picked[0]
    for w in picked[1:]:
        pw += (rng.choice(chars) if len(chars) > 1 else chars) + w
    out.append({"kind": "memorable", "words": words, "separator": sep, "capitalised": cap, "password": pw})
# Safari: three groups of six, letters and digits, one capital somewhere; Chrome: 15 letters and digits.
safari = "-".join(
    "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(6)) for _ in range(3)
)
safari = safari[:4] + safari[4].upper() + safari[5:]
out.append({"kind": "safari", "password": safari})
out.append(
    {
        "kind": "chrome",
        "password": "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(15)),
    }
)
out.append({"kind": "phrase", "password": "the kettle hums at dawn"})
out.append({"kind": "emoji", "password": "☕️ espresso 🫘 crema"})
out.append({"kind": "unicode", "password": "café crème brûlée"})
print(json.dumps(out, ensure_ascii=False, indent=1))
