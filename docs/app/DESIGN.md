# Shot-logging app: design

The plan is in [PLAN.md](PLAN.md). Mockups for the six key screens are in [mockups/](mockups/), each as a
self-contained HTML file with light and dark screenshots at 390 px. All coffees, prices and shots in them are
sample data.

## How this was made

Six designers each wrote a direction for the same brief, working independently. Each followed one community
design skill, read from its repository as reference material. The brief described the app, the owner at the
machine at 7am with one hand free, the six screens, and the existing site's look (warm off-white, near-black, a
brown accent). The skills:

| Skill | Repository | Read |
|---|---|---|
| apple-design | emilkowalski/skills | `skills/apple-design`, with `emil-design-eng` and `mobile-native` |
| web-design-engineer | ConardLi/garden-skills | `skills/web-design-engineer` and its Dieter Rams style recipe |
| landing-page-design | elayadesign/ai-design-skills | `skills/landing-page-design` |
| build-awwwards-quality-sites | MengTo/Skills | `web-design/build-awwwards-quality-sites`, read with `ui/no-ai-design-slop` |
| better-ui and friends | jakubkrehel/skills | `better-interface`, `better-ui`, `better-colors`, `better-typography` |
| UI and interaction principles | Owl-Listener/designer-skills | `color-system`, `dark-mode-design`, `typography-scale`, `fitts-law`, `form-design`, `gesture-patterns`, `feedback-patterns`, `interfaces-that-feel` |

The brief placed build-awwwards-quality-sites in Owl-Listener/designer-skills. That repo doesn't have it; the skill
is in MengTo/Skills. Owl-Listener's repo is a set of UI and interaction principle skills, so its designer worked
from those instead. All six repositories could be reached.

## The six directions

| Skill | Direction | Type | Surfaces | Taste colours | Signature | Repeat shot |
|---|---|---|---|---|---|---|
| apple-design | (still being written; added in the next commit) | | | | | |
| web-design-engineer | "Group Head": the app as a Braun control panel. Spec sheets, hairlines, uppercase labels, one brown signal dot | Archivo, JetBrains Mono | flat, 2–4 px radius, no shadows | a brown ramp: light brown sour, accent brown balanced, dark brown bitter | an extraction gauge, 15 to 40 s with the window marked and a needle | 3 taps |
| landing-page-design | "One pull": every screen is a landing page with one conversion | Geist, Geist Mono | flat, 16 px cards, one translucent nav | amber, green, brick red | the advice set large as a two-line tagline that reveals word by word | 3 taps |
| awwwards + no-slop | "Pull Sheet": a barista's dial-in sheet; four huge numbers, the bag photo as identity | Newsreader, Barlow Condensed, Barlow | flat, proximity instead of cards | ochre, green, rust | a line drawn from dose to yield as the ratio settles, once, on save | 3 to 5 taps |
| Krehel | "Bench Scale": a good digital scale; precise, restrained, one serif for the coffee name | system-ui, Newsreader | white cards with shadow rings, 20 px concentric radius | citrus yellow, green, slate blue (no red: red means error) | none; the one expressive moment is a staggered entrance on save | 3 taps |
| Owl-Listener | "Bench Scale": an instrument with one trusted readout and detented controls | Atkinson Hyperlegible Next, IBM Plex Mono | flat, tonal, hairline borders | amber, green, brick red, each with an arrow and a word | a ratio bar with the 1:2 tick and the 25 to 32 s band | 3 taps, plus a 5 s undo |

### Where they agree

All six came to the same handful of conclusions without seeing each other's work:

- The numbers are the hero. Dose, yield, time and ratio are set very large in tabular figures, like an instrument
  readout. Nothing decorative competes with them.
- Keep the warm paper ground and the brown accent from the site, and make dark mode a warm near-black, not grey.
- The only image is the owner's own bag photo. No stock beans, no illustrated cups, no emoji.
- Steppers, not dials or sliders. Free-spinning dials and precision sliders are hard with a wet thumb, and a
  gesture-only control has no fallback.
- Numerals change instantly when stepped. "An instrument doesn't lag."
- One primary action per screen, full width, in the thumb zone. Targets of 56 px or more.
- Prefill everything from the last shot of the coffee except taste (and, for most, rating). A repeat shot is three
  taps.
- Taste is never shown by colour alone. Each state carries a word, and most added a shape or an arrow.
- Capture is a full-bleed camera, then a match sheet with one confirm button and a quiet "not this one".
- Corrections are new records, shown honestly. Undo writes a retraction.

### Where they disagree, and what I chose

| Question | Positions | Choice and why |
|---|---|---|
| Typeface | Geist; Atkinson + Plex Mono; Newsreader + Barlow Condensed; system-ui + Newsreader; Archivo + JetBrains Mono | Archivo for everything functional, using its width axis: 100% for labels, 72% for numerals, so four-digit values fit beside 56 px buttons. Newsreader only for coffee names and the advice sentence. A condensed numeral is what lets the log screen keep big numbers and big buttons on one row. The serif makes the coffee the subject and the numbers the tool, as two directions argued independently. |
| Depth | shadow rings (Krehel); flat with hairlines (the other five) | Flat with hairlines. Shadows on a phone in a dim kitchen read as smudges, and spec rows don't need cards. |
| Radius | 2–4 px (Rams); 10 px (site); 16–20 px | 8 px on controls and buttons, 6 px on photos, 14 px on the top of a sheet. Squarer than the soft-app default but less cold than the Rams recipe. |
| Taste colours | traffic-light; brown ramp; slate-blue bitter | Citrus for sour, sage green for balanced, a plum for bitter. A brown ramp is clever but puts "balanced" on the accent colour, so the Save button and a tag would look alike. Red would read as an error. Plum stays clear of both and still reads as "too far". Each tag also has a mark: a hollow circle for sour, a filled circle for balanced, a square for bitter. |
| Labels | sentence case (three); tracked uppercase (two) | Sentence case at 13 px. Tracked uppercase eyebrows are one of the tells the no-slop skill names, and they're harder to read half awake. |
| Signature element | ratio bar; extraction gauge; dose-to-yield line; word-by-word tagline; none | The dial-in target: time across, ratio up, the 25–32 s × 1:1.8–2.3 window shaded, each shot of the bag as a mark, joined oldest to newest. It merges the ratio bar and the extraction gauge into the one picture that explains the advice, and it's real data, so it repeats on Today, the log screen, the saved screen and the coffee page. The word-by-word reveal is cut: it delays the one thing you want to read. |
| Shot form layout | chips select one big active value (Owl); 2×2 grid plus a scrub strip (MengTo); stacked rows (Krehel, Rams, landing) | Stacked rows: dose, yield, time, grind, each with its numeral and its own − and + on the right. Everything is visible at once, and a repeat shot is mostly reading. The scrub strip is a nice extra for later but needs the buttons anyway. |
| Advice applying itself | one tap applies it (MengTo); never edits the value (Owl) | The grind field keeps the last value and shows the suggestion next to it. One tap takes the suggestion. Advice that edits your recipe silently would be wrong at least sometimes. |
| Motion on save | staggered entrance; spring settle; line draw; none | One movement: the new mark travels from the previous shot's position on the target to its own, 360 ms, then the advice fades in. It shows the change you made. Nothing else on the screen moves. |
| Dark mode default | follow the system; dark between 5 and 8 am | Follow the system. A clock-based theme surprises people. |
| Rating prefill | empty (most); last value (one) | Empty, for now. Open question 7 in the plan. |

The chosen direction keeps the name two directions picked independently: **Bench Scale**. The app is a counter
instrument: a paper-coloured page, hairline rules, big condensed numerals, a serif name for the coffee, one brown
action, and a dial-in target that shows where the shot landed.

## Tokens

### Colour

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#f6f5f2` | `#14110f` | page |
| `--panel` | `#ece9e2` | `#1d1916` | grouped background, rarely used |
| `--sheet` | `#fffdf9` | `#221e1a` | bottom sheets, stepper buttons |
| `--rule` | `#dcd6cc` | `#342e28` | hairlines |
| `--rule-strong` | `#c4bcb0` | `#4a423a` | control borders, axes |
| `--ink` | `#1b1714` | `#efe8de` | text, numerals |
| `--ink-2` | `#5f574e` | `#a99e91` | labels, secondary text (≥ 6:1 on paper) |
| `--ink-3` | `#8a8075` | `#7c7166` | gridlines and the dotted shot path; never text (3.5:1) |
| `--accent` | `#8a4b1d` | `#e0a46e` | the one primary action, links, the signal dot |
| `--accent-press` | `#6f3c17` | `#c98d58` | pressed primary |
| `--on-accent` | `#fff8f0` | `#1c1208` | text on accent |
| `--sour` / `--sour-bg` | `#7a5a00` / `#f5eac6` | `#e6c45c` / `#2c2511` | sour tag and segment |
| `--balanced` / `--balanced-bg` | `#2d6a45` / `#e0ede3` | `#86c79c` / `#15261b` | balanced; the target window |
| `--bitter` / `--bitter-bg` | `#7f2f4c` / `#f3e0e7` | `#e99ab4` / `#2f1820` | bitter |
| `--window` | `rgba(45,106,69,.10)` | `rgba(134,199,156,.10)` | target window fill |

Light keeps the site's paper and accent exactly, so the app and the public site look related. Dark is warm and a
step less contrasty than pure white on black, for a dark kitchen. Every text pair is at least 4.5:1; the taste
text-on-tint pairs are at least 5.3:1 in light and 7.6:1 in dark.

### Type

| Role | Family | Size / weight | Notes |
|---|---|---|---|
| Hero numeral (saved ratio) | Archivo, width 72% | 112 / 500 | tabular figures, tracking −0.01em, line height 1 |
| Field numeral | Archivo, width 72% | 56 / 500 | |
| Summary numerals | Archivo, width 72% | 34–44 / 500 | |
| Units | Archivo, width 85% | 18–20 / 400 | `--ink-2`, sits on the numeral's baseline |
| Coffee name | Newsreader | 24–34 / 500 | optical sizing on |
| Advice sentence | Newsreader | 24 / 500, line height 1.25 | |
| Body | Archivo | 16 / 400 | inputs never below 16 px, so iOS doesn't zoom |
| Button | Archivo | 17 / 600 | |
| Label | Archivo | 13 / 500, sentence case | `--ink-2` |
| Chart text | Archivo, width 85% | 11 / 500 | `--ink-2` |

Both families come from Google Fonts. Load only the axes used (`Archivo:wdth,wght@62..125,400..700`,
`Newsreader:opsz,wght@6..72,400..600`).

### Spacing and size

- 4 px base. Steps used: 4, 8, 12, 16, 24, 32.
- 16 px side gutter.
- Field rows are at least 80 px tall. Stepper buttons are 56 × 56 with an 8 px gap. Primary buttons are 60 px tall
  and full width.
- Rating pips are 52 px tall. Taste segments are 56 px.
- The bottom dock respects the home-indicator safe area (`env(safe-area-inset-bottom)`; 34 px in the mockups).
- Use `100dvh`, not `100vh`, so the dock doesn't hide behind the browser bar.

### Motion

| What | Duration | Easing |
|---|---|---|
| Press (buttons, steppers) | 100 ms, scale 0.97 | ease-out |
| Stepper value change | instant | none |
| Segment selection | 120 ms, background colour only | ease-out |
| Sheet in / out | 240 ms / 180 ms | `cubic-bezier(0.2, 0, 0, 1)` |
| Saved: new mark moves to its place on the target | 360 ms | same curve, no overshoot |
| Saved: advice fades in | 200 ms, 120 ms after the mark lands | ease-out |
| Undo bar countdown | 5 s, linear | the only linear motion |

Never animated: numerals, lists, navigation, the running timer (it ticks), first render. With
`prefers-reduced-motion: reduce`, everything is an instant swap or a plain crossfade. No `transition: all`.

Haptics: one light tick per stepper step and on save, where the browser supports `navigator.vibrate` (Android).
iOS Safari doesn't, so nothing there depends on it.

## Components

**Field row.** Label above, numeral with unit below, steppers on the right. The label can carry a hint chip ("was
12") when the value differs from the last shot or advice suggests a change; tapping the chip applies it. Long-press
on a stepper repeats after 400 ms, every 80 ms. Tapping the numeral opens a numeric keypad for big jumps. A
left-hand setting mirrors the steppers to the left edge.

**Time field.** The same row, with a Start/Stop button in place of the steppers. While the timer runs, the numeral
ticks once a second and the button reads Stop. After stopping, the steppers come back for corrections.

**Taste segments.** Three equal segments, each with the word and what it means for the grind ("grind finer",
"keep", "grind coarser"). Selected: tinted background, 2 px inset ring in the taste colour. No default selection.

**Rating pips.** Five squares, 1 to 5. Selected is filled with `--ink`.

**Taste tag.** For lists: the word in the taste colour on its tint, with the shape mark. Never colour alone.

**Dial-in target.** An SVG, x from 18 to 38 s, y from 1:1.5 to 1:2.7, gridlines at 20/25/30/35 s and 1:1.6/2.0/2.4.
The window is a dashed rectangle with a light fill and the word "window". Marks per shot by taste shape; the
newest shot gets a ring. On Today and Saved the marks are joined oldest to newest with a dotted line, so the dial-in
path is visible. On the coffee page they aren't joined (too many), and a legend sits under it. It has an
`aria-label`, and the text under it says the same thing in words.

**Spec sheet.** Two columns, label 112 px and value, hairline between rows. Used for the match card, the catalog
facts and equipment. Numbers in values use the condensed numeral.

**Bag photo.** 6 px radius with a 1 px inner outline so a pale bag doesn't melt into the paper. In the mockups it's
an honest hatched placeholder; in the app it's the owner's photo, cropped to the label, dimmed slightly in dark mode.

**Dock.** The bottom area holding the primary button (and on Saved, the undo bar). Paper background, a hairline
above it, safe-area padding below.

**Navigation.** Three text tabs (Today, Coffees, Equipment) with the accent dot above the active one. No icon set is
needed, and the words are clearer than pictograms for "equipment".

**Sync status.** Top right: a green dot and "Synced", or the accent dot and "2 waiting to sync". It never blocks
anything.

**Undo bar.** An inverted strip in the dock: "Shot saved" and "Undo", with a thin line underneath that shrinks over
5 seconds. Undo writes a retraction record.

## Copy

Short, plain and specific. Buttons start with a verb. Advice says what happened and what to do: "Ran fast at 22 s.
Go finer: 12 → 11." Errors give a way forward: "Couldn't read the bag. Retake it with the label facing the light,
or search by roaster." No exclamation marks, no "Great shot!".

## What's not designed yet

Search (the fallback from a wrong match), History, the reading skeleton on Capture, empty states (first run, no
bags yet), the left-hand layout, and the numeric keypad. They use the same components.
