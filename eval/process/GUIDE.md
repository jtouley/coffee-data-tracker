# Labelling guide: a coffee listing's process

You see one product listing at a time: its title, its tags, its variant names, the top of its description and
every later passage of the description that could be about processing. No prediction is shown. Give the one label
that says how **this product's coffee cherries were processed, as the listing states it**. Use only what the listing
says; never what is typical for the country or the roaster.

## Labels

| Label | When |
|---|---|
| `washed` | The listing says washed, fully washed, double washed, wet process(ed), "lavado", or describes pulping, fermenting and washing the beans before drying. |
| `natural` | The listing says natural, natural process(ed), naturally processed, dry process(ed), full natural, or describes drying the whole cherry. "Sun-dried" alone (no other method named) is natural. |
| `honey` | Honey (any colour: black, red, yellow, white honey), honey process(ed), pulped natural, "miel", and semi-washed outside Indonesia. |
| `anaerobic` | The listing says anaerobic (anaerobic natural, anaerobic washed, honey anaerobic …) or carbonic maceration for this coffee. Anaerobic wins over the base method it is paired with. |
| `other` | A stated process that is none of the above: wet-hulled / giling basah, Indonesian "semi-washed", or a named fermentation with **no** base method stated (lactic, yeast-inoculated, koji, co-fermented, fruit-infused, thermal shock, "double fermentation", "experimental process"). |
| `multiple` | The product states more than one process for its coffee: a blend or pack whose components differ ("Process: Natural & Washed"), a choice of lots, or two contradictory statements. |
| `none` | The listing states no process for this coffee. |

## Rules

1. **A named fermentation plus a base method is the base method** ("72-hour double fermentation natural" is `natural`;
   "co-fermented washed" is `washed`), unless the fermentation is anaerobic or carbonic maceration (`anaerobic`).
2. **Drying is not the process.** "Washed and sun-dried on raised beds" is `washed`; "dried on patios" alone is `none`.
3. **Decaffeination is not the process.** Swiss Water, Mountain Water, sugarcane / EA, CO2 and "natural
   decaffeination" say nothing about the cherry. "Washed, then Swiss Water decaffeinated" is `washed`.
4. **Flavour words are not processes.** "Notes of honey", "natural sweetness", "naturally sweet", "honeysuckle" are not
   a process. "Processed at the X washing station" is not a process either (naturals are made at washing stations too).
5. **Blends and multi-coffee products.** Label the process only when the listing states one process for the whole
   product ("all components are washed"). Different processes for different components is `multiple`; a process
   stated for only one component of several is `multiple`.
6. **Another coffee mentioned for contrast** ("unlike our washed lot, this one is natural") doesn't count: label the
   product's own coffee. If you can't tell which coffee a statement is about, use `multiple`.
7. **Variant names count** when every variant is the same product: variants "Washed / 12 oz" and "Natural / 12 oz"
   make the product `multiple`; a single "Natural" variant makes it `natural`.
8. **Not sure between two labels?** Choose `none` if the listing never clearly says a process, `multiple` if it says
   two. Never guess from origin ("Ethiopian coffees are usually washed").

## Output

One line per listing: `<key> <label>`, nothing else. Don't look at other labellers' answers or at any prediction file.
