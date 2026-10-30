# Shot-logging app: design

The plan is in [PLAN.md](PLAN.md). Mockups for thirteen screens are in [mockups/](mockups/), each a self-contained
HTML file with light and dark screenshots at 390 px. They show the owner's equipment (DF64 V2, Lelit Bianca,
Pullman basket); the coffees, prices and shots are sample data.

## How this was made

Six designers each wrote a direction for the same brief, working independently. Five returned a written
direction. The apple-design designer didn't finish, so I read that skill myself and filled in its row from the
skill text. It is a motion and interaction skill more than a visual one, so some columns don't apply. Each followed one community
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
| apple-design | Fluid and physical: respond on press, critically damped springs, sheets you can grab, translucent chrome with scroll-edge fades, system font first, size-specific tracking | system font (SF on iPhone) | translucent bars and sheets over content | not addressed | none; motion that starts from the current value and can be interrupted | not addressed |
| web-design-engineer | "Group Head": the app as a Braun control panel. Spec sheets, hairlines, uppercase labels, one brown signal dot | Archivo, JetBrains Mono | flat, 2–4 px radius, no shadows | a brown ramp: light brown sour, accent brown balanced, dark brown bitter | an extraction gauge, 15 to 40 s with the window marked and a needle | 3 taps |
| landing-page-design | "One pull": every screen is a landing page with one conversion | Geist, Geist Mono | flat, 16 px cards, one translucent nav | amber, green, brick red | the advice set large as a two-line tagline that reveals word by word | 3 taps |
| awwwards + no-slop | "Pull Sheet": a barista's dial-in sheet; four huge numbers, the bag photo as identity | Newsreader, Barlow Condensed, Barlow | flat, proximity instead of cards | ochre, green, rust | a line drawn from dose to yield as the ratio settles, once, on save | 3 to 5 taps |
| Krehel | "Bench Scale": a good digital scale; precise, restrained, one serif for the coffee name | system-ui, Newsreader | white cards with shadow rings, 20 px concentric radius | citrus yellow, green, slate blue (no red: red means error) | none; the one expressive moment is a staggered entrance on save | 3 taps |
| Owl-Listener | "Bench Scale": an instrument with one trusted readout and detented controls | Atkinson Hyperlegible Next, IBM Plex Mono | flat, tonal, hairline borders | amber, green, brick red, each with an arrow and a word | a ratio bar with the 1:2 tick and the 25 to 32 s band | 3 taps, plus a 5 s undo |

### Where they agree

All six came to the same handful of conclusions without seeing each other's work:

- Dose, yield, time and ratio are the largest things on screen, set in tabular figures like an instrument readout.
  Nothing decorative competes with them.
- Keep the warm paper ground and the brown accent from the site, and make dark mode a warm near-black.
- The only image is the owner's own bag photo. No stock beans, no illustrated cups, no emoji. One exception the owner asked for: the line-drawn cup on the Reading state.
- Steppers for every number. Free-spinning dials and precision sliders are hard with a wet thumb, and a
  gesture-only control has no fallback.
- Numerals change instantly when stepped. "An instrument doesn't lag."
- One primary action per screen, full width, in the thumb zone. Targets of 56 px or more.
- Prefill everything from the last shot of the coffee except taste (and, for most, rating). A repeat shot is three
  taps.
- Taste is never shown by colour alone. Each state carries a word, and most added a shape or an arrow.
- Capture is a full-bleed camera, then a match sheet with one confirm button and a quiet "not this one".
- Corrections are new records and are shown as corrections. Undo writes a retraction.

### Where they disagree, and what I chose

| Question | Positions | Choice and why |
|---|---|---|
| Typeface | Geist; Atkinson + Plex Mono; Newsreader + Barlow Condensed; system-ui + Newsreader; Archivo + JetBrains Mono | Archivo for everything functional, using its width axis: 100% for labels, 72% for numerals, so four-digit values fit beside 56 px buttons. Newsreader only for coffee names and the shot summary. A condensed numeral is what lets the log screen keep big numbers and big buttons on one row. The serif makes the coffee the subject and the numbers the tool, as two directions argued independently. |
| Depth | shadow rings (Krehel); flat with hairlines (the other five) | Flat with hairlines. Shadows on a phone in a dim kitchen read as smudges, and spec rows don't need cards. |
| Radius | 2–4 px (Rams); 10 px (site); 16–20 px | 8 px on controls and buttons, 6 px on photos, 14 px on the top of a sheet. Squarer than the soft-app default but less cold than the Rams recipe. |
| Taste colours | traffic-light; brown ramp; slate-blue bitter | Citrus for sour, sage green for balanced, a plum for bitter. A brown ramp is clever but puts "balanced" on the accent colour, so the Save button and a tag would look alike. Red would read as an error. Plum stays clear of both and still reads as "too far". Each tag also has a mark: a hollow circle for sour, a filled circle for balanced, a square for bitter. |
| Labels | sentence case (three); tracked uppercase (two) | Sentence case at 13 px. Tracked uppercase eyebrows are one of the tells the no-slop skill names, and they're harder to read half awake. |
| Signature element | ratio bar; extraction gauge; dose-to-yield line; word-by-word tagline; none | The dial-in target: time across, ratio up, the 25–32 s × 1:1.8–2.3 window shaded, each shot of the bag as a mark, joined oldest to newest. It merges the ratio bar and the extraction gauge into the one picture that shows how a bag is dialing in, and it's real data, so it repeats on Today, the log screen, the saved screen and the coffee page. The word-by-word reveal is cut: it delays the one thing you want to read. |
| Shot form layout | chips select one big active value (Owl); 2×2 grid plus a scrub strip (MengTo); stacked rows (Krehel, Rams, landing) | Stacked rows: dose, yield, time, grind, each with its numeral and its own − and + on the right. Everything is visible at once, and a repeat shot is mostly reading. The scrub strip is a nice extra for later but needs the buttons anyway. |
| Advice applying itself | one tap applies it (MengTo); never edits the value (Owl) | Moot for now: the owner deferred all grind suggestions until a model trained on their shots exists (PLAN, "Later: a dial-in model"). The grind field shows the last shot's value and nothing else. |
| Motion on save | staggered entrance; spring settle; line draw; none | One movement: the new mark travels from the previous shot's position on the target to its own, 360 ms, then the summary line fades in. It shows the change you made. Nothing else on the screen moves. |
| Dark mode default | follow the system; dark between 5 and 8 am | Follow the system. A clock-based theme surprises people. |
| Rating prefill | empty (most); last value (one) | The owner decided: taste and rating start empty, and a "Same as last" chip fills both from the previous shot in one tap. Every rating stays a choice, and a repeat shot is still three taps. |

From apple-design I took the parts that suit a utility: feedback on press instead of on release, sheets that
follow the finger and settle with a critically damped spring, scroll-edge fades instead of hard rules under
floating bars, tracking that tightens as type grows, and undo for slips instead of confirmation dialogs. I left out
its translucent materials (glass over a paper page in a dark kitchen adds glare and nothing else) and its default
to the system font (the condensed numeral is doing real work here).

The chosen direction keeps the name two directions picked independently: Bench Scale. The app is a counter
instrument: a paper-coloured page, hairline rules, big condensed numerals, a serif name for the coffee, one brown
action, and a dial-in target that shows where the shot landed.

## Director pass

After the first set of mockups I went through every screen again, taking from each skill the rule it put best. What
changed, and which rule drove it:

| Change | Rule | Source |
|---|---|---|
| Removed the dial-in target from the log screen. It now appears on Today, Saved and the coffee page only, and it is largest on Saved. On the log screen it cost 118 px and pushed rating below the fold. | "Prefer one strong authored moment over many unrelated effects." | MengTo, no-ai-design-slop |
| Added the reading state (a skeleton of the match card, "Type it instead") and the first run ("Nothing on the grinder yet"). | Every state is designed; skeletons over spinners; no blank empty states. | elayadesign; Krehel |
| The undo strip on Saved was an inverted bar, bright in dark mode. It's now a quiet strip in the sheet colour with an accent "Undo". | Avoid abrupt brightness jumps; don't put bright surfaces in a dark kitchen. | apple-design §14; Owl-Listener dark-mode |
| The dock lost its top hairline and gained a 20 px fade where content scrolls under it. | Scroll-edge effects instead of hard dividers under floating chrome. | apple-design §12 |
| A grabber on the match sheet, so it reads as something you can pull down. | Familiarity; sheets you can grab. | apple-design §3, §16 |
| Tracking now changes with size: −0.03em on the 112 px ratio, −0.015em on other numerals, +0.01em on 13 px labels. Serif headings use `text-wrap: balance`. | Tracking is size-specific, never one value. | apple-design §15; Krehel better-typography |
| Field rows got 10 px of space above the label so the label belongs to its number, not the rule above it. | Proximity implies relationship. | apple-design §16; Owl-Listener |
| The line under the target on Today is now set in the serif, like on Saved, so the shot summary always has the same voice. | Things that look the same behave the same. | apple-design §16 |
| "11, lower is finer" became "11 of 90". Direction lives in the grinder profile. | Concise, specific labels. | Krehel better-writing |
| Chart text moved from `--ink-3` (3.5:1) to `--ink-2` (6.5:1). | Every text pair meets 4.5:1. | Krehel better-colors; Owl-Listener color-system |
| Mockups now have a plain status bar so they read as phone screens. | | |

Things I considered and left alone: a scrub strip for values (MengTo, Owl-Listener) is a good later addition, but the
buttons have to exist anyway; the left-hand mirror (Owl-Listener) is a setting and needs no mockup of its own; Braun-style
uppercase labels (web-design-engineer) stay out for the reason given above.

## Re-review

After accounts, invites and the domain were added, six reviewers each went over all eleven screens of the time and this
document against one skill: apple-design, Krehel's skills, MengTo's no-slop audit, Owl-Listener's heuristic
evaluation, web-design-engineer and landing-page-design. They worked independently and didn't see each other's
notes. They agreed far more than they disagreed. What they found, and what changed:

| Finding | Raised by | Change |
|---|---|---|
| The sample data contradicted itself. Log shot said the last shot was 27 s with a "was 12" chip while Today said 29 s at grind 11; "Pull again" said 36 g against 36.4 g elsewhere; "$2.62 / 100 g" for a 340 g bag at $22 is wrong by 2.5×. | all six | One dataset across every screen (listed at the top of the generator). The price is $6.47 / 100 g. No hint chip when nothing changed. |
| The time was prefilled from the last shot, so skipping the timer saved a stale time and fed it to the advice. | Owl-Listener (severity 4) | Time starts as "Not timed · last 29 s". Saving without a time turns the button into "Save with 29 s?" for one more tap. |
| Destructive actions looked routine: "Sign out everywhere" in the accent like "Invite someone", "Revoke" in the bitter plum. | five | A `--danger` token, used only for destructive actions and errors. "Sign out on all devices" and "Revoke invite" use it, without a chevron, and are undoable. Taste colours mean taste only. |
| Outlined pills meant "tap me" on Log shot and "status" on Setup and Invite. | four | Outlined chips are always tappable. Status ("owner", "member", "not used yet") is plain 13 px text after the name. "Default" is gone until there are two of a kind. |
| In dark mode the full-width `#e0a46e` button was the brightest thing on screen, after the director pass had removed the bright undo bar for that reason. | apple-design, Owl-Listener, web-design-engineer | A separate `--accent-fill` (`#9a5524` in dark, 5.4:1 with its text). `#e0a46e` stays for text links. |
| Twenty-two font sizes, spacing off the grid, four radii nobody declared, about a hundred inline styles. Page titles were serif on three screens and sans on Setup. | web-design-engineer, apple-design, MengTo, Krehel | The scale is 12 13 15 17 20 24 34 44 56 112; spacing 4 8 12 16 24 32 48; radii 4 6 8 14. Every page title is the serif at 34 px. The generator uses classes, not inline sizes. |
| Control borders (`#c4bcb0`) were 1.7:1 against the paper, below the 3:1 non-text minimum. `--ink-3` was used for text in tags and placeholders. | Owl-Listener, Krehel, web-design-engineer | `--control` (3.3:1 or better) for every control border. `--ink-3` appears nowhere as text. Chart text is 12 px. |
| No way out of the camera; no read-failed state; no password recovery, no Show toggle, no error copy on sign-in. | apple-design, Owl-Listener, landing-page-design | × and Retake on the camera, a read-failed screen, Show in password fields, "Ask jtouley for a reset code" on sign-in, and error copy in Components below. |
| An invitee landed on the owner's sign-in screen; Copy and Share competed as equal buttons. | landing-page-design | Share sends `app.registry.coffee/join#<code>`, which opens "jtouley invited you" with the code filled in. Share link is the one primary action; Copy link is a text button inside the code box. |
| The dotted path on the target ran back over itself along one line, so the order couldn't be read. | Owl-Listener, Krehel, landing-page-design | Marks are numbered in order, with a paper halo behind each number; the path is 1.5 px `--ink-2`. The sample shots spread in ratio as real ones would. |
| Jargon: "score 94", "EXIF removed", the basket's SKU, "1 bag read today of 10". The Reading screen said "only the label text is used", which wasn't true: the image goes to Claude. | four | A confident match shows no score; a weak one asks "Which one is it?" and shows each candidate's score ("match 68"), where the number helps. No read time and no "EXIF" on the camera; the Reading screen says once, in plain words, that the photo goes to Claude with its location removed. No SKU; "1 of 10 bag reads used today". |
| The scroll-edge fade sat inside the opaque dock, so it could never show, and the tab bar still had a hairline. | apple-design | The fade sits 20 px above the dock and the tab bar, over the content; no hairline. Sign in turns it off because the button follows the field directly. |
| The two advice lines said different things in different sizes. | apple-design, Krehel | One string in the serif at 24 px on Today and Saved. It now describes the shot ("Inside the window, two seconds slower than Monday’s shot") since grind suggestions were deferred. |
| Rating 1 to 5 had no anchor. | Owl-Listener | "worst" and "best" under 1 and 5. |
| No manual theme switch. | Owl-Listener | Setup has Theme: Same as phone, Light, Dark. |
| The wordmark said "Coffee Data" while the address will be registry.coffee. | landing-page-design | Sign in and Sign up use "registry.coffee". registry.coffee is the product name; renaming the repo and the public dataset is the owner's call. |

Reviewer suggestions I didn't take:

- Two typefaces (landing-page-design allows one). The condensed numeral and the serif name do different jobs.
- Sizes and radii aren't Tailwind's scale (landing-page-design). They're this app's own declared scale.
- No word-by-word reveal (landing-page-design calls it mandatory). It delays the one thing you want to read.
- The undo countdown is linear (landing-page-design forbids default curves). An eased countdown would misstate the
  time left.
- Press feedback stays at 100 ms and 0.97 (apple-design) instead of Krehel's 150 ms and 0.96; the shorter press feels
  quicker.
- No Braun-style uppercase labels (web-design-engineer's Rams recipe). Sentence case reads better half awake.

## Tokens

### Colour

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#f6f5f2` | `#14110f` | page |
| `--panel` | `#ece9e2` | `#1d1916` | skeleton blocks |
| `--sheet` | `#fffdf9` | `#221e1a` | sheets, inputs, stepper buttons |
| `--rule` | `#dcd6cc` | `#342e28` | hairlines between rows (not control edges) |
| `--control` | `#8f8578` | `#7a6f63` | every control's border, the chart axis (≥ 3.3:1 on paper and sheet) |
| `--ink` | `#1b1714` | `#efe8de` | text, numerals |
| `--ink-2` | `#5f574e` | `#a99e91` | labels, secondary text, chart text (≥ 6.3:1) |
| `--ink-3` | `#8a8075` | `#7c7166` | nothing that has to be read; reserved |
| `--accent` | `#8a4b1d` | `#e0a46e` | text you can tap: back links, text buttons, chips; the active-tab dot |
| `--accent-fill` | `#8a4b1d` | `#9a5524` | the one primary button per screen |
| `--on-accent` | `#fff8f0` | `#fff8f0` | text on `--accent-fill` (6.4:1 light, 5.4:1 dark) |
| `--danger` | `#b3261e` | `#f2836f` | destructive actions and errors only (6.0:1, 7.4:1) |
| `--sour` / `--sour-bg` | `#7a5a00` / `#f5eac6` | `#e6c45c` / `#2c2511` | sour |
| `--balanced` / `--balanced-bg` | `#2d6a45` / `#e0ede3` | `#86c79c` / `#15261b` | balanced; the target window |
| `--bitter` / `--bitter-bg` | `#7f2f4c` / `#f3e0e7` | `#e99ab4` / `#2f1820` | bitter |
| `--window` | `rgba(45,106,69,.10)` | `rgba(134,199,156,.10)` | target window fill |
| `--camera` / `--on-camera` | `#1a1714` / `#f2ece4` | `#0b0a09` / `#f2ece4` | the camera view and its controls |
| `--photo-a` / `--photo-b` | `#d9cfc0` / `#cfc3b2` | `#2b2621` / `#26211c` | the photo placeholder in mockups only |

One colour, one meaning. Taste colours appear only on taste. Green isn't used for "synced" or "valid" (those are a
check mark in ink). The accent marks what you can tap; nothing static uses it. With `prefers-contrast: more`,
`--rule` takes the value of `--control`.

### Type

The scale is 12, 13, 15, 17, 20, 24, 34, 44, 56 and 112 px. Nothing else.

| Role | Family | Size / weight | Tracking, leading |
|---|---|---|---|
| Hero numeral (Saved ratio) | Archivo, width 72% | 112 / 500 | −0.03em, 1 |
| Field numeral | Archivo, width 72% | 56 / 500 | −0.015em, 1 |
| Summary numerals, invite code | Archivo, width 72% | 44 or 34 / 500 | −0.015em, 1 |
| List numerals | Archivo, width 72% | 20 / 500 | −0.015em |
| Units | Archivo, width 85% | 20 beside 56 and 44, 17 beside 34 and 24, 13 beside 20 | `--ink-2`, on the baseline |
| Wordmark | Newsreader | 44 / 500 | −0.02em, 1 |
| Page title | Newsreader | 34 / 500 | −0.02em, 1.1 |
| Coffee name, shot summary | Newsreader | 24 / 500 | 0, 1.15 to 1.25 |
| Row title, button, input | Archivo | 17 / 600 (inputs 400) | 0 |
| Body | Archivo | 15 / 400 | 0, 1.45 |
| Label | Archivo | 13 / 500 | +0.01em |
| Section label | Archivo | 13 / 600 | 0 |
| Chart text, anchors, photo caption | Archivo | 12 / 500 | 0 |

In the app, sizes are written in rem so they follow Android's font-size setting. At 130% and above, the field row
stacks its steppers under the numeral instead of beside it.

### Spacing, size and shape

- Spacing steps: 4, 8, 12, 16, 24, 32, 48. Side gutter 16.
- Radii: 4 for chips and taste tags, 6 for photos, 8 for buttons, inputs and controls, 14 for the top of a sheet.
- Tap targets: at least 48 × 48, including back links and text buttons. Field rows at least 88 tall; stepper buttons
  56 × 56 with 8 between; primary buttons 60 tall and full width; taste segments 56; rating pips 52.
- The dock and the tab bar respect the gesture-bar safe area (`env(safe-area-inset-bottom)`; 32 in the mockups).
  Use `100dvh`, not `100vh`.

### Motion

| What | How |
|---|---|
| Press (buttons, steppers) | on pointer-down, scale 0.97, 100 ms ease-out; not on disabled buttons |
| Stepper value change | instant |
| Segment and pip selection, including "Same as last" filling them | background colour, 120 ms ease-out, with the 8 ms haptic tick |
| Sheet in / out | spring, damping 1.0, response 0.3 s; rises from and returns to the bottom edge; follows the finger when dragged |
| Saved: the new mark moves to its place on the target | spring, damping 1.0, response 0.36 s |
| Saved: the summary line fades in | 200 ms ease-out, after the mark settles |
| Undo countdown | 5 s, linear (an eased countdown would misstate the time left) |
| Reading: the cup fills | 5 s ease-out to 85%, then holds; the stream drips every 0.5 s, linear, until the read ends. Reduced motion: still, half full |

Never animated: numerals, lists, navigation, the running timer (it ticks), first render. With
`prefers-reduced-motion: reduce`, every movement becomes an instant swap or a short crossfade. No `transition: all`.

Haptics (Android Chrome): an 8 ms tick per stepper step and selection, 12 ms when the timer starts and stops, two
short ticks on save, fired on the same frame as the visual change.

## Components

#### Field row

Label above, numeral and unit below, steppers on the right. Values come from the last shot of the bag; the app never
changes one on its own and doesn't suggest a different grind. Long-press on a stepper repeats after 400 ms, every 80 ms. Tapping the numeral opens a
numeric keypad. A left-hand setting mirrors the steppers to the left edge.

#### Time field

Starts empty: "Not timed · last 29 s". Start/Stop (120 wide) and Type (56) sit where the steppers
are. While the timer runs, the numeral ticks once a second and the button reads Stop. Saving with no time changes
the Save button to "Save with 29 s?"; a second tap saves with the last time, recorded as copied
(`time_source` = `copied`) so it never counts as measured.

#### Same as last

An accent chip on the Taste row: "Same as last · Balanced, 4". One tap fills the taste segment and
the rating pip, visibly, with a tick. Tapping another segment or pip afterwards overrides it. Hidden when there's no
earlier shot of this coffee.

#### Taste segments

Three equal segments: Sour, Balanced, Bitter. No sublabels about grind. Selected: tinted background and a 2 px inset ring in the taste colour. No default.

#### Rating pips

Five squares, 1 to 5, "worst" and "best" under the ends. None selected by default; selected is
filled with `--ink`.

#### Taste tag

In lists: the word on its tint with the shape mark (hollow circle sour, filled circle balanced, square
bitter). On the coffee page's chart legend, the same marks with plain `--ink-2` words and no tint, so the legend
doesn't look like a row of filters.

#### Dial-in target

SVG, time across and ratio up. By default it runs 18 to 38 s and 1:1.6 to 1:2.5, with gridlines at 20/25/30/35 s and
1:1.8/2.0/2.2/2.4. When a shown shot falls outside that range (a 1:5 shot, a 60 s shot, a 1:1 ristretto), the axes
grow to include it, never below 0 s, with at most five round gridlines on each grown axis; a shot is plotted where it
was pulled, never pinned to the edge. The window is a dashed rectangle with a light fill and the word "window" inside
its lower-right corner when the box is at least 24 px tall and that corner is clear; otherwise above, below, right or
left of the box, in the first spot clear of every mark, the newest shot's ring, the path and the order numbers, and
left out if none is clear. On Today and Saved, the marks are numbered in the order pulled and joined by a dashed 1.5
px `--ink-2` line, and the newest has a ring. On the coffee page they're neither numbered nor joined. It has an
`aria-label`, and the sentence under it says the same thing in words.

#### Chips

Outlined, 44 tall, 8 radius. Accent outline for actions ("Same as last"); `--control` outline
with ink text for a picker (the grinder and basket chip). Never used for status.

#### Status text

Role or state after a name, in 13 px `--ink-2` with no border: "jtouley owner", "Invite from Oct
24 not used yet".

#### List link

A 56-tall row with a hairline below, 17 px ink text and an ink-2 chevron when it opens something.
Actions that happen in place (Sign out) have no chevron. Destructive ones are `--danger`, have no chevron, and give
a 5 s undo strip instead of a confirmation dialog.

#### Spec sheet

Two columns, label 112 px wide in `--ink-2` and value, hairline between rows. For the match card,
catalog facts and nothing else.

#### Text input

56 tall, 17 px text, `--sheet` background, `--control` border, 8 radius. The label sits above it and
stays; hints and errors sit below in 13 px, errors in `--danger`. Focus: accent border plus a 3 px accent ring at
22%. Password fields have a 48-wide "Show" button inside. `autocomplete` is `username`, `current-password` or
`new-password`, so Android's password manager fills and saves them. Errors never clear what was typed.

#### Invite code

Four groups of four in Crockford base32 (the digits and capitals without I, L, O and U, so 16 characters carry
80 bits), shown at 44 px across two lines in a dashed box, with the link under it and a "Copy link" text button. The
input on Sign up accepts it with or without dashes, in any case, reads O as 0 and I or L as 1, and checks it as soon
as it's complete.

#### Match card

The sheet after a confident read (top score at least 80 and at least 5 ahead of the next, the rule `shots.log_shot`
already uses): "Found in the catalog", the coffee name as the title, roaster, then a spec sheet of roast date (marked
"read from the bag"), origin, notes, price and stock. No score, no read time. "Use this coffee" and "Pick another
match".

#### Weak match

When the read isn't confident: the title "Which one is it?", one sentence saying what the bag said, and up to three
candidate rows with name, origin, process, stock and the score as "match 68" in `--ink-2`. The score appears only
here, where it helps choose. Rows are the actions; the dock holds "None of these: type the coffee".

#### Camera

Full-bleed, `--camera` background, a corner-bracket frame for the bag, × at top left and Retake at top
right (both 48 × 48, `--on-camera`). A status line at the bottom left appears only while reading ("Reading the
label") or after a failed read.

#### Sheet

14 px top corners, a 36 × 5 grabber, `--sheet` background. Dragging down past a third of its height
dismisses it back to the camera (Retake).

#### Reading state

The match card's shape as skeleton blocks (no spinner, no shimmer), "Looking for roaster, coffee
and roast date", the expected wait, the privacy line, and "Type the coffee" so nobody waits on the model.
Beside the wait, a 40 px line-drawn cup in `--ink-2` fills over the expected 5 s and holds at 85% (never "done"
before the read is), with a dripping stream: transforms only. Reduced motion: a still, half-full cup. The owner
asked for it after first use, when the pulsing skeleton alone didn't read as working.

#### Bag photo

6 radius with a 1 px inner outline so a pale bag doesn't melt into the paper. Dimmed slightly in dark
mode. In the mockups it's a hatched placeholder.

#### Dock

The bottom area holding the primary button and at most one text button under it. Paper (or sheet)
background, a 20 px fade above it where content scrolls under, safe-area padding below, no hairline.

#### Navigation

Three text tabs, Today, Coffees and Setup. The active one is ink, weight 600, with the accent dot
above it; the others are `--ink-2`. A 20 px fade above, no hairline. Sign in, Sign up and the camera have no tabs.

#### Sync status

Top right: a check and "Synced", or "2 waiting to sync" in `--ink-2`. It never blocks anything.

#### Undo strip

`--panel`, radius 8, no border: "Shot saved" and an accent "Undo", with a thin line under Undo
that shrinks over 5 s. Undo writes a retraction record.

#### Edit bag

An accent "Edit bag" text link (56 px tall, ending on the gutter) after the roast line on Today and on the coffee
page. The form: roaster and coffee, roast date, weight, and "Still using it" / "Finished". Saving writes a new record
under the same id (the shots stay with it) and returns to the screen it was opened from, with a notice. A roast date
in the future isn't saved; one more than 90 days old asks "Save with day N?".

#### Notice

What the last save did, at the top of the screen it lands on: "Bag saved: Onyx Geometry", "DF64 V2 saved",
"Correction saved", "Shot undone", "Password changed". `--panel`, radius 8, like the Undo strip; `role="status"`,
so it's read out (shown empty, then filled, so screen readers announce it). A save queued offline says so: "Saved on
this phone. It’ll sync when you’re back online."; a bag names itself, since Today may still show the last one until
it syncs: "Bag saved on this phone: Onyx Geometry. It’ll sync when you’re back online." Logging a shot keeps Saved as its confirmation, with Undo.

Every save leaves its form: it returns to the screen the form was opened from (Setup, first run, the gear picker
on Log shot), stepping back in history, or else replaces the form, so Back never reopens what was just saved.

## Copy

Short, plain and specific, in sentence case, with real apostrophes (’). Buttons start with a verb. The same action
has the same words everywhere ("Type the coffee", never "Type it instead"). The app says what happened and leaves
the next move to the owner: "Ran fast at 22 s, outside the window." "Inside the window, two seconds slower than
Monday’s shot." No "go finer" or "try 11" until the dial-in model in the plan exists.

Errors say what happened and what to do next, and keep what was typed:

| Where | Copy |
|---|---|
| Sign in, wrong password or username | That username and password don’t match. Check them, or ask jtouley for a reset code. |
| Sign in, locked | Too many tries. Try again in 15 minutes, or ask jtouley for a reset code. |
| Sign up, code used or expired | This invite has already been used. Ask jtouley for a new one. / This invite expired on November 3. |
| Sign up, username taken | Someone already has that username. Try another. |
| Sign up, short password | Use at least 10 characters. |
| Weak match | The bag says Larkspur, “Hambela”, roasted Oct 18. None of these is a close match, so pick the right one. |
| Reading failed | The label was too dark or out of focus. Retake it with the label facing the light, or type the coffee. |
| Daily read limit | You’ve used today’s 10 bag reads. Type the coffee, or try again tomorrow. |
| Offline save | Saved on this phone. It’ll sync when you’re back online. |

The share text for an invite: "Log your espresso shots with me on registry.coffee: app.registry.coffee/join#K7QD-M2XF-9RPA-T4WE
(works once, until November 3)."

## What's not designed yet

Search (from "Pick another match" and "Type the coffee"), History, the reset-code entry screen, the left-hand
layout, the numeric keypad, and the offline and camera-denied states. They use the components above.
