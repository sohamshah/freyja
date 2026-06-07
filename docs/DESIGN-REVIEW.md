# Freyja Design Review + Vision

A living doc with three parts:

1. **The method** — how we do a design review, what to look for, in what order.
2. **The vision** — Freyja's vibe thesis and how it should evolve.
3. **The reviews** — specific screens/surfaces evaluated against the method, with the vision as the yardstick. Appended over time.

The method and vision sit at the top as the standing reference. Each review at the bottom links back to them — "this screen failed Pass 2 because X; it drifts off-vibe because Y."

---

## Part 1 — The method

The trap of UI review is to confuse *I have opinions* with *I have a method*. Experts don't trust their gut alone — they have **passes**, each tuned to surface a different class of problem. The passes are done in order because each one biases your perception, and starting with the wrong one ruins the rest.

### Setup before you look

See it cold. Close the app, reopen, and pay attention to the first 3 seconds. That window is the only time you get a real first-impression read before your brain rationalizes it. Take a screenshot the moment something feels off — past-you is the most honest critic.

Then look at it three more ways:

- Full screen at native size.
- **Squinted** — out-of-focus eyes, color blocks and weights only.
- As a **phone screenshot** scrolled past quickly.

Different perceptual modes catch different things.

### Pass 1 — Vibe (3 seconds, adjectives only)

Before you can articulate anything, write down the adjectives. *Crisp. Loud. Anxious. Competent. Default. Alive. Dead. Tasteful. Designed. Generic.* The adjective list is the spec — you're going to walk it back and find what produced each one.

Diagnostic questions for vibe:

- **Who made this?** Does it look like one person with taste, or three committees and a checklist? Coherent products feel single-authored even when they aren't.
- **What era is this?** Skeuomorphic 2010, flat 2014, neumorphic 2020, expressive-type 2024. Mixing eras within one screen is uncanny — that's where "doesn't feel right" usually lives.
- **Where's the human?** Personality is in microcopy, empty states, error messages, animation curves. A product with zero human voice in any of those reads like SaaS-template no matter how clean the type is.
- **Would I screenshot this for inspiration?** If the honest answer is no, there's a vibe ceiling and the rest of the review is moving deck chairs.

### Pass 2 — Eye-path

Where does your eye land first? Is that what it should be looking at? Trace the path it takes from there. If two things scream "look at me," neither wins. If nothing screams, the screen has no point of view.

Specifics:

- Can you find the primary action in <2s?
- Is the primary CTA visually heavier than secondary ones, or just colored differently? (Color isn't weight.)
- Are there competing focal points — loud icon next to loud heading next to loud button?
- Visual hierarchy should be a **staircase**, not a podium with three #1s.

### Pass 3 — Functional walkthrough

Now go through the main journeys with intent, not just the happy path. The signature of a polished product is that all the **dead states are designed**: empty states, loading states, error states, the in-between states when something takes >300ms.

Things to specifically look at:

- **Empty states** — is there a designed empty, or is it just absence?
- **Error messages** — do they tell you what to do, or just what broke?
- **Loading** — acknowledged or just spinner-shaped silence?
- **Hover and focus** — every clickable thing; none should feel inert.
- **Microcopy on disabled buttons** — does it explain *why* disabled?
- **Double-click / fast clicks** — what happens if you click the same thing twice fast?
- **Content stress** — a 2-paragraph item title; does the layout hold?
- **Sparse content** — an empty list; does it collapse weirdly?

### Pass 4 — Aesthetic discipline

This is where you count things.

- **How many type sizes are on screen?** (If you can't tell at a glance, there are too many.)
- How many font weights?
- Are sizes from a system (12/14/16/20/24/32) or bespoke?
- **How many colors are doing real semantic work** vs decoration?
- Are border weights consistent (1px hairline, 1.5px structural, 2px emphasis) or random?
- Corner radii from a token set or eyeballed?
- Shadows from one elevation scale or floating-depth chaos?

**The killer test**: grayscale the screenshot. If the hierarchy still works in grayscale, the design is structural; if everything collapses into a gray soup, color was doing hierarchy's job.

### Pass 5 — Density and breath

Does it breathe? Whitespace should be doing work, not just absent. Two failure modes:

- **Cramped + busy** — every pixel begging for attention, nothing wins. Common in dashboards.
- **Empty + evasive** — vast whitespace, single element floating, "minimalist" as cover for not having content figured out. Common in landing pages.

Great dense UIs (Bloomberg, Linear, Figma) earn density: every line carries signal. Great sparse UIs (Stripe checkout, Apple settings) earn sparsity: every element is decisive. Bad UIs in both directions feel **defensive** — like the designer was afraid to commit.

### The diagnostic discomfort signal

The most useful muscle: when something bothers you and you can't say why, **don't dismiss it.** That unnameable discomfort is the diagnosis trying to surface. Sit with it. Send a screenshot to a friend with no caption. Sleep on it. The named version usually arrives within 24 hours; the unnamed version is the open ticket.

Junior reviewers say "I don't know, something feels off." Senior reviewers say the same thing but write it down and keep digging until they can name it. The difference is the writing-it-down.

### Vocabulary you need

You can't fix what you can't name. Build working fluency in:

- **Typography**: weight, size, leading (line-height), tracking (letter-spacing), optical vs geometric alignment, font-feature-settings.
- **Spacing**: token scale (4/8/12/16/24/32/48), padding vs margin, baseline rhythm.
- **Color**: HSL not just hex, "tinted neutrals" (greys that lean toward your primary), semantic colors, state colors.
- **Motion**: easing curves (linear is the smell of "default"), duration ranges (snappy <200ms, deliberate ~300ms, contemplative >400ms), what's animating and *why*.
- **Composition**: focal point, visual weight, grid alignment, optical centering ≠ geometric centering for non-symmetric shapes (e.g. play triangles, arrowheads).

Without the vocabulary, you can only say "it looks off." With it, you can say "the leading is too tight for the size — 24px text on 28px line-height reads cramped; bump to 32." Now it's a fix.

### Expert tricks

- **The squint test**, done seriously: blur your eyes until you only see color blocks and weights. The thing that's wrong shows up immediately because detail noise disappears.
- **Grayscale screenshot**: tests whether color is a crutch.
- **Phone screenshot scrolled past quickly**: catches scale and touch-target issues you don't feel on a 27" monitor.
- **5-second test**: show someone the screen for 5 seconds, hide it, ask what they remember. The recall is the actual hierarchy.
- **Walk-away test**: look at it tomorrow morning before reading email. The trained eye is sharpest unfatigued.
- **Side-by-side with an aspirational reference**: not to copy, to calibrate. If the work looks juvenile next to Linear or Cron or Things, name why — specifically: density discipline, hover sweetness, motion subtlety, color restraint.

---

## Part 2 — The vision

"Agentic workspace" is too soft to do work. It's accurate but it doesn't tell you what to add or cut. Sharper:

### What Freyja actually is

Walk through the surface and the language tells you everything: *mission dashboard, swarm, gateway, harness, judge, dispatcher, autopilot, runtime, kanban, sub-agent profiles, session pid, REQUEST CONTEXT 32k/200k, coordination strategy.*

That's not workspace vocabulary. That's **cockpit / instrument / operations** vocabulary. The user isn't using the product, they're *operating* it. They aren't a customer of agents; they're the commander.

The aesthetic agrees:

- Departure Mono + Geist Mono everywhere — a *code editor reading* of a UI, not a chat reading.
- All-lowercase tracking-spaced section headers, monospace numerals — the typographic vocabulary of synthesizers and terminals.
- Topographic line backdrops, vector field canvas, shiny fabric WebGL — atmospheric *technical*, not atmospheric pretty. Reminds you you're inside a machine.
- The PID showing in the gateway badge is the giveaway. Consumer apps hide pids. Freyja makes them part of the chrome — **the internals are the product**.
- The sleeping-dogs idle video is the master move. It's the one piece of warmth in an otherwise instrument-grade UI, and it's *off-axis* warmth — not cute branding, not a mascot, just an unexpected human gesture. That single asset does more for the vibe than five hundred CSS variables.

### The sharpened thesis

> **An agent operator's workshop. Instrumented and atmospheric, complexity-as-feature, made by a person who lives in it.**

Decoded:

- **Workshop**, not workspace — there's making happening, not just consuming.
- **Operator's**, not user's — agency over a fleet, not over a conversation.
- **Instrumented** — pids, tokens, ticks, verdicts surfaced as first-class.
- **Atmospheric** — the backdrops, the fonts, the sleeping dogs aren't decoration; they signal who this is for and what mood they're in.
- **Complexity-as-feature** — sub-agents and coordination strategies aren't hidden behind "Ask AI"; they're exposed as a control surface.
- **Made by a person** — the thing actual operators can smell from a mile away, and the only durable moat against the wave of generic agent UIs.

### What's already pulling in the right direction

- The coordination-strategy mode picker (bus / isolated / kanban / goal). Operating *modes* is operator vocabulary.
- Sub-agent profile cards with `model / thinking / tools / max iterations / source` exposed. Instrument-grade transparency. Don't soften it.
- The judge-deep loop with auditable verdicts + rework iterations. Showing the machinery is the vibe.
- The compaction cooperation language — `[ctx: NN% · advisory]`, `<system-reminder>`. A *protocol*, treated like one. Most apps would hide context pressure; Freyja makes it part of the conversation.
- File_path:line_number convention in the activity rail.
- The Slack/Telegram gateway extending the operator's reach beyond the desktop.
- The dog video.

### Where it's drifting and worth tightening

1. **The mission dashboard tab stack is getting busy.** Health / tasks / activity / profiles + per-strategy overrides — when tabs need to be filtered per mode, the IA is straining. An operator's instrument has one or two primary surfaces and everything else is on-demand. The tab pile reads as "we wanted everything available," which is the consumer-SaaS instinct showing. Worth a pass to find the *primary gauge* of each mode and make the rest peripheral.

2. **Generative UI widgets (`show_widget`).** Big risk vector. If the agent's widgets default to "rounded card, friendly heading, blue CTA," the entire consumer-SaaS visual language gets imported into the cockpit. The widget design system needs to be opinionated on the cockpit side — monospace numerals, hairline borders, terminal-adjacent — so an agent can't accidentally render Stripe-checkout-style UI inside the instrument panel.

3. **Sub-agent profile metaphor.** Right now they're "profiles" with personalities (explore, judge-deep, browser-qa, performance). One step further toward "agent characters" and it starts smelling like Discord bots or Slack apps. Keep the framing as **instrument settings** — "judge-deep" is a *tool configuration*, not a *character*. Labels matter: "profile" stays cold; "agent persona" would be warm and wrong.

4. **The kanban view has Jira-shaped genes.** Cards + lanes + auto-dispatch + review pipeline — classically project-management territory, and the visual language of every PM tool is the same anti-vibe. The way out is to make the board look and read like a *control panel*, not a project tool. Status as glyph not pill, dispatcher tick as a live readout not a banner, judge verdicts as inline criterion grids not card comments.

5. **Onboarding tone.** The highest-stakes vibe surface. The first 10 seconds either set the user up as "this is a serious instrument; pay attention" or "this is another AI assistant; let me explain." Most products fail this because they apologize for their own complexity. Freyja's whole thesis is that the complexity is the feature — onboarding should *show* a sub-agent spawning, the coordination strategy switching, the judge issuing a verdict, and trust the user to find it interesting.

6. **The Norse / mythological naming bench.** Freyja, Hermes (deep-dive), judge, drafter. Beware drift into RPG cosplay. The way to keep it good is to use these names *operationally* (a Hermes deep-dive is a kind of research pass) not *characterfully* (Hermes is your wise companion). The line is thin and very mockable when crossed.

### Evolution moves

- **A persistent "what's running" header strip.** Bloomberg-style ticker showing live state: active sub-agents, current coordination mode, autopilot status, context pressure, session spend. Always visible. Cockpits don't hide their gauges.

- **A "watch mode."** When the agent is doing long autonomous work (autopilot dispatching cards, goal loop iterating), an ambient screensaver-grade view that's beautiful enough to leave on a second monitor. The vector field backdrop hints at this — push it harder. The aspiration: a tool that looks alive at rest.

- **Operator macros / loops as first-class.** Not "templates." Not "workflows." **Macros** — named, parameterized, replayable sequences the operator wires up themselves. Names matter, and "macro" is correct for the vibe in a way "workflow" never can be.

- **Restraint on iconography.** Most products lean on icon families (Lucide, Phosphor), which makes them look identical. Mono-typography section markers + glyph numerals are doing most of the work already; resist the urge to icon-everything. The text *is* the instrument.

- **One signature interaction nobody else has.** The dog-video idle is precedent. Pick one more: the sound of dispatch ticks, the way a sub-agent spawn animates in, the way the judge verdict reveals criteria one at a time. One genuinely original moment per session is the difference between "another AI app" and "Freyja."

- **A point of view on the user.** "Agent operator" is who, but the writing should reflect *what kind*. Lean toward people who run their own infra, build their own tools, edit their own dotfiles. Not "everyone." A product with a specific user is legible; a product for everyone is invisible.

### The diagnostic question to evolve by

Every new screen, feature, copy decision — ask:

> **Does this make the user feel like an operator at a serious instrument, or like a customer of an AI service?**

If it's the second, drifting. The first answer is the vibe. The second is the wave being stood out from.

---

## Part 3 — Reviews

Appended over time. Each entry walks the 5 passes against a specific surface and scores it against the vision's diagnostic question. Format:

```
### [date] — [surface name]

Pass 1 — Vibe: ...
Pass 2 — Eye-path: ...
Pass 3 — Functional: ...
Pass 4 — Aesthetic: ...
Pass 5 — Density: ...

Diagnostic: operator or customer? ...

Punch list:
- ...
- ...
```

---

### 2026-06-02 — First-run / onboarding + active session viewer

Two adjacent surfaces reviewed together: the first-run path (Splash → HeroWelcome → first send) and the active session viewer (what happens once the turn is in flight). Run as three parallel deep-reads: onboarding surface, session viewer, and a hard-numbers audit of the aesthetic system that grounds both. This section consolidates the findings — for raw structured returns see `/tmp/freyja-review.json` (workflow `wf_f65abfa5-654`).

The reviewer is reading code, not running the app — runtime UX is inferred from JSX, CSS, motion configs, and copy. Items marked *needs live verification* couldn't be settled from source alone.

---

#### Aesthetic system inventory — ground truth

Before pass-by-pass: the system is **three competing partial systems, not one**. This shapes every aesthetic finding below.

**Layer 1 — small intentional token core (`tailwind.config.cjs`).** 5 bg + 5 fg + 3 accent + 4 status colors, one accent box-shadow, 8 kanban keyframes, one font-size token (`2xs`). Explicit philosophy in comments ("Steel-blue accent is the only saturated color in the palette"). Spacing is the *default* Tailwind 4px scale, not extended.

**Layer 2 — a 1,046-line bespoke CSS layer (`src/renderer/styles/globals.css`).** Owns the glass ladder (`.glass` → `.glass-strong` → `.glass-raised` → `.menu-opaque` → `.modal-opaque`), the hairline system, prose styling, kanban surfaces, council-tile/hive-card pulses, sticky-section-chip morph, splash/idle treatments. Internally consistent and carefully engineered, but lives outside the token system.

**Layer 3 — sprawl of inline overrides in components.** This is where the drift lives:

| Drift | Count | Worst examples |
|---|---|---|
| Arbitrary text sizes (`text-[Npx]`) | **1,137** | text-[10px] (255), text-[11px] (184), text-[10.5px] (184), text-[9px] (134) — half-pixel scale steps prove it's eyeballed per-element |
| Arbitrary spacing tokens | **306** | `py-[2px]`, `gap-[3px]`, `min-h-[260px]`, `top-[52px]` etc. — `tailwind.config.cjs` adds zero spacing extensions |
| Raw `rgba()` / `rgb()` literals | **151** | `rgba(168,212,252,*)` (accent) hard-typed **33×** in components + **28×** in globals.css |
| Bespoke off-palette hex backgrounds | ~10 | `#0a0a0e`, `#08080c`, `#11111a`, `#13131c`, `#15151b`, `#171b20` in ArtifactWorkspace, SwarmMonitor, ToolTimeline, AppErrorBoundary — none match `bg-0..bg-4` |
| Bespoke radii | 14 | `rounded-[1px..18px]` overlap with `rounded-md/lg/xl/2xl` aliases — no documented hierarchy |
| Per-agent profile colors (saturated) | ~13 hues | `#5bbb5b`, `#6ba3d6`, `#ffcc66`, `#f5b45d`, `#7fb8e8`, `#f07878`, `#e0a040`, `#d99bbe`, `#88d67f`, `#7ab8a3`, `#b080d0`… (council-tile + hive-card CSS variables) |
| Unique motion durations (untokenized) | 13 | 120 / 140 / 160 / 220 / 240 / 280 / 360 / 720 / 1000 / 1100 / 2400 / 2600ms + IdleSleep & ShinyFabric one-off keyframes |
| Font weights drift | `font-light` (11), `font-normal` (32), `font-medium` (12), `font-bold` (19) — including `font-light` on 8-10px mono where the thin weight barely renders |

**Where the system is strong** — these are real differentiators worth defending:

- **Atmospheric assets are signature-grade**: TopoBackdrop (marching-squares + ridged-FBM perturbation, hypsometric HSL tint), VectorFieldBackdrop (vertical-biased curl field with iron-filings ticks), ShinyFabricBackdrop (Bayer-dithered WebGL satin), IdleSleep (sleeping-dogs portal with measured baked-corner color `rgb(0,4,4)` + mixBlendMode:lighten radial mask + 8s halo breathe). Each has IntersectionObserver / resize / prefers-reduced-motion handling. None is decoration; all are engineered.
- **Frosted-glass ladder is disciplined**: alpha 0.18 → 0.94 across 7 variants, each pairs with a matching inset hairline + drop-shadow. `ring-hairline` alone is used 175 times.
- **Single saturated color rule holds** in the chrome — steel-blue `#a8d4fc` is the only saturated hue in the bg/fg/accent palette. Status colors (ok `#a8b0a8` sage, warn `#b8a078` ochre, danger `#b48282` rose) are deliberately desaturated. This is rare discipline.
- **Mono digits + stylistic sets** used as identity: `ss01` globally, `ss02` + `ss03` + `cv11` on `.md`, `tabular-nums` on telemetry rows. Real, not accidental.
- **Zero third-party icon library**. Glyphs are bespoke inline SVGs at small fixed grids (`viewBox 0 0 10 10` / `0 0 14 14` / `0 0 12 12`). The "no AI-generic-iconography" character is preserved.
- **Kanban motion subsystem IS tokenized**: 8 dedicated keyframes (drop-in, complete-glow, active-pulse, progress-flow, verdict-pass/fail, judge-thinking) with consistent `cubic-bezier(0.16, 1, 0.3, 1)` easing. This is the model the rest of the motion system should follow.

**Lurking sins**:

- Both `Departure Mono` and `Geist Mono` are still loaded. Tailwind maps `sans`/`mono`/`display` all to Geist with a comment "nothing new should opt into Departure Mono", yet `TopoWordmark.tsx` and the `.ascii` / `.scanline` / `.label` utilities still reach for Departure. The cleanup never finished.
- Fraunces (the editorial serif for hero h1s) is **not self-hosted** — no `@font-face`, relies on a Google Fonts `<link>` or system fallback to Georgia.
- `globals.css .ascii` is defined but appears unused in the active-session components — possible dead utility from an earlier wordmark approach.
- `SubagentCard.tsx:555-569` has an `AGENT_TYPE_COLORS` Record mapping 13 agent types to *the exact same* string — vestigial scaffolding from a deliberately-removed color-coded variant.

---

#### Surface 1 — First-run / onboarding

**Files inspected**: `App.tsx`, `SplashScreen.tsx` (613 lines), `HeroWelcome.tsx`, `Conversation.tsx` (mount path only), `InputDock.tsx`, `ModelPicker.tsx`, `SettingsModal.tsx`, `ComputerPermissionWizard.tsx`, `SlackSetupWizard.tsx`, `TitleBar.tsx`, `globals.css`, `tailwind.config.cjs`.

##### Pass 1 — Vibe

Adjectives: **atmospheric · instrumented · hand-tuned · cinematic · monochrome-with-one-blue · muttering-to-itself · engineer-aesthetic · show-off-y.**

- **Who made this?** Unmistakably one person who lives in the app. The shader comments in `SplashScreen.tsx` ("Visual vocabulary verbatim from the pearl boot reference"; the two-channel argument about why `iconGp` must not release or rings would re-expand during the icon fade-out) are an author's notebook, not a designer's spec.
- **Era**: 2024–2026 post-Linear/Granola/Raycast — frosted-dark glass with one steel-blue accent, mono everywhere, Departure/Geist typography. With a strong Bloomberg Terminal undertone and an Apple-keynote-bumper splash.
- **Human presence**: strong. Triple-Esc emergency-stop comment apologizes for why ⌘Esc had to replace bare Esc (`App.tsx:464-467`). Settings "yolo" tier label. SlackSetup re-entrancy ref comment. Splash's "starting gun easing" remark. The author is curating, not shipping.
- **Screenshot-for-inspiration**: yes.

**Pass-1 verdict**: *On first launch this is a workshop, not a chatbot. The pearl-shader splash is genuinely arresting and the hero is restrained. The vibe is right. The risk is that the splash writes a check the hero state doesn't quite cash — after 14 seconds of cinema, you land on a near-empty room with one button labeled "open mission dashboard" and a kbd hint.*

##### Pass 2 — Eye-path

**Splash**: dead-center pearl ripple → topographic mark crystallizes at viewport center around t=3s.
**Post-splash**: topographic mark stays at viewport center; two cards + a kbd hint resolve in the bottom strip.

**Intended focal point**: the input dock at the bottom — that's the action.
**Actual hierarchy shape**: **broken (podium with 3+ #1s).**

Competing focal points:
- The topographic mark at viewport center — biggest, most animated, *does nothing*. It is identity.
- Workspace card + Quick start card (glass-raised, accent-dot bullets) — orientation.
- The cradle with its `❯` prompt and focused-state cyan ring — action.
- Title bar: seven+ controls on a 46px strip (workspace, dashboard, metrics, model, strategy×4, focus, ctx, spend, activity, gateway pill) — too many parallel readouts.

**Primary action findable in <2s**: yes — auto-focus on mount (`InputDock.tsx:102-104`) + the accent `❯` glyph carry it. But the Quick start card's only CTA ("open mission dashboard") evangelizes a dashboard that is also empty on first run. Circular nudge.

##### Pass 3 — Functional walkthrough

**Empty states**:

| Context | Designed | Note |
|---|---|---|
| First-run conversation (no messages) | ✓ (generic) | Designed but the geometry is generic-AI-app shape. Workspace card + Quick start card + tip line. No suggested prompts; no starter row; no signposting to `/computer`, `/learn-this`, ⌘K. |
| Workspace path unknown at first paint | ✓ | `"detecting workspace..."` — good operator microcopy, but no follow-up if detection genuinely fails (HeroWelcome.tsx:96). |
| Mission dashboard reached from first-run | ✗ | The only first-run CTA points to a dashboard whose tiles (jobs/runs/metrics) are empty before any session has run. High likelihood of dead-end. *Needs live verification.* |
| Model picker with bridge offline / no API keys | ✓ (partial) | FALLBACK_MODELS list (good); unavailable rows show red ✕ + "API_KEY unset" (good). But clicking an unavailable model only fires a toast — no inline "add your key" affordance. SettingsModal has no key-management section either. |

**Loading states**:

| Context | Designed | Note |
|---|---|---|
| Splash → first paint | ✓ | 14s WebGL pearl ripple, skippable on any non-modifier key/click after a 600ms guard. *No on-screen skip affordance* — invisible shortcut. |
| Bridge boot during splash | ✗ | App.tsx fires bridge handshake (getMode, hydrateFromDisk, hydrateSettings, scheduler init) in parallel with the splash; no visible bridge-status indication while splash is up. If boot fails, user sees the dissolving splash → near-empty hero → tiny "demo"/"offline" pill in title bar that's easy to miss. |
| Model warming / first stream | ✗ (partial) | Streaming reported in chrome (braille spinner top-right + cancel pill in dock), but the **conversation column is silent** between submit and first token — could be seconds on a cold cloud model. |

**Error states**:

| Context | Designed | Note |
|---|---|---|
| Missing API key on send | ✗ | Picker-level toast exists. **No first-run pedagogy** (go to Settings → API keys), and SettingsModal *has no API-key section*. |
| Bridge fails to boot | ✗ | BridgeStatus pill shows red "offline" with `title={modeDetail}` — tooltip-only. No inline banner; easy to miss. |
| Permission denied on computer-use | ✓ | `ComputerPermissionWizard` is genuinely strong. The "Critical: macOS silently privacy-filters the screen" callout (lines 165-177) is best-in-class operator candor. |
| No model configured at all | ✗ | InputDock will accept input regardless; sends to whatever the default model is. No hard gate. |
| No harness CLI installed | ✓ | Harness rows render with red ✕ + reason inline. Fails noisily, not silently. |

**Microcopy smells** (specific fixes):

- `HeroWelcome.tsx:101-109` — "Start from the input below, or open the current session map." → "Type below to start, or press ⌘K for the command palette. Try `/computer` for desktop control or `@` to attach files."
- `HeroWelcome.tsx:113-117` — tip teaches `⌘⇧M` (mission dashboard hotkey). First-run tip should teach **conversation primitives** (`⌘K`, `/`, `@`, `/computer`) — those are what new operators need.
- `InputDock.tsx:473-479` placeholder is already strong; add a one-line concrete example below on first run only (`e.g. /computer find a flight to Tokyo next Tuesday`).
- `ComputerPermissionWizard.tsx:82-87` has two dismissal affordances within 200px (header "skip" + step-0 footer "not now"). Pick one.
- `ModelPicker.tsx:411` — "⏎ preview · effort chip selects · esc close". Enter **selects + closes**, not previews. Use "⏎ use".
- `SettingsModal.tsx:53` — "Auto-approve everything -- yolo" uses double-dash as faux em-dash. Either use a real em-dash or just "(yolo)".

**Stress findings** (the no-config first-run path):

- Cold launch with **no API keys**: splash plays its 14s film, hero resolves to two glass cards + kbd hint, user types a message, and *only then* learns the model isn't usable. No first-run sentinel says "pick a model first" or "add an API key first". The ModelPicker is never auto-opened.
- Cold launch with **no harness CLI** + first-run user picks "Codex" runtime: the picker shows the unavailable reason, but the user has to *go looking* for it.
- Cold launch into **demo mode** (no Electron bridge / browser-opened): App.tsx:236 starts an in-renderer demo driver and shows "demo" in a 5-letter title-bar pill. Fully functional UI that quietly isn't talking to anything real.
- **prefers-reduced-motion**: only neutralizes the canvas-enter fade (`globals.css:1039-1045`). The 13-second WebGL shader **still runs**. A reduced-motion user gets the full 14 seconds.
- **Splash skip discoverability**: skip is in the code (line 563+) but there's no on-screen "press any key" hint. New users sit through the full 14s the first time, every time.

##### Pass 4 — Aesthetic discipline

- **Type sizes seen on first-run surfaces**: ~12 distinct sizes between 8.5px and 15px. Several are 0.5px apart (`9` vs `9.5`, `10` vs `10.5`) — collapsible.
- **Distinct colors used**: full bg/fg/accent/state palette + the **freyja wordmark gradient** (`#7ae6ff → #a8d4fc → #b4b0ff → #c89aff`, TitleBar.tsx:82-91) which is the only non-palette color in the chrome. This single gradient is also the most saturated element in the entire UI.
- **Border weights**: 0.5px sub-pixel insets / 1px hairline / 1.2px hive-card stroke / 1.4px TopographicMark — consistent vocabulary.
- **Radii**: 2 / 3 / 4 / 5 / 6 / 7 / 10 / 12 / 16 / 18 / 999px — **proliferation**. Some are functionally identical (`rounded-md ≈ 6px`, `rounded-[6px]`).
- **Shadow layers**: hairline insets / ring-hairline-strong / glass ladder (escalating opacity) / cradle-focused (5-layer composite) / session-pane-active (4-layer) / `title-status-dot` (`0 0 7px currentColor` — the only saturated glow in the chrome, earned).
- **Grayscale test**: **passes**. The hierarchy survives a desaturate filter.
- **Token discipline**: **system-leaning** at this surface — chrome respects the palette. The wordmark gradient is the lone exception.

##### Pass 5 — Density and breath

| Region | Density | Earned? | Note |
|---|---|---|---|
| Splash (t=0–13s) | empty | ✓ | Full-screen WebGL membrane + single icon. Empty by design — this is the only moment a Freyja user should not be doing anything. |
| Hero-empty area (post-splash) | **evasive** | ✗ | The hero icon is huge and inert. Two ~280px glass cards + one tip line — comically sparse for a tool that promises complexity-as-feature. **No recent sessions, no skills inventory, no scheduled-jobs preview, no last-touched workspace, no model+permission status.** The room is dressed but no instruments are out. The chrome above is showing readouts; the hero below should mirror that density. |
| Input dock at empty state | balanced | ✓ | `❯` + textarea + reveal-on-focus hint row + workspace/model readout. The hint-row opacity trick is sharp. |
| Top title bar | **cramped** | ✗ | Seven+ controls on a 46px strip; on first run every numeric readout is at zero. Instruments out, all reading null — visual noise without value. |
| Model picker modal | balanced | ✓ | 560px wide, scrollable. Each model row carries label / tier / family / thinking glyph / id / ctx / reasoning / availability. The right amount of density. |
| Settings modal | balanced | ✓ | Two sections (permissions tiers, computer control). Pedagogical without being preachy. |
| Computer permission wizard | balanced | ✓ | Four steps with critical/tip callouts. The macOS privacy-filtering callout is best-in-class. |

**Overall**: the chrome and modals are appropriately dense. **The hero empty state is the outlier — too quiet for a tool that bills itself as an instrument.**

##### Diagnostic — operator or customer?

**Score: mixed_lean_operator.**

The splash, chrome, cradle, model picker, permission wizard, and microcopy voice all read operator. The Computer Permission wizard alone — with its "macOS silently lies to you" callout and the dev-tip about TCC grants surviving `npm run start` — is operator-coded at a level few tools attempt. The diagnostic answers "instrument" for ~80% of the surfaces.

Where it slips toward customer is **the hero empty state**: a centered icon, two cards, one CTA pointing to a circular dashboard, and a single kbd tip. That's the geometry of a Notion-AI welcome panel, not an operator console. The splash promises a workshop; the hero delivers a foyer.

##### References

- Linear (palette restraint, hairline dividers, cradle focused-glow) — positive
- Granola (one steel-blue accent on near-monochrome) — positive
- Bloomberg Terminal (instrumented chrome strip, readouts everywhere) — positive
- Raycast / Arc / Cron (cinematic-but-restrained boot) — positive
- Apple keynote bumpers (the pearl-substrate splash is in this neighborhood) — positive but possibly too cinematic for an everyday boot
- **ChatGPT default web** (centered icon + suggestion cards) — *negative drift, HeroWelcome's geometry*
- **Notion AI panel** (frosted-glass empty with one CTA) — *negative drift, same complaint*

##### Punch list (severity-ordered)

| # | Sev | Surface | Finding | Fix |
|---|---|---|---|---|
| O-1 | **S2** | HeroWelcome empty state (`HeroWelcome.tsx:91-129`) | Geometric twin of ChatGPT default — centered icon, two cards, one CTA to a dashboard that's also empty. Loudest off-vibe drift in the surface. | Replace with an instrumented panel: workspace + git branch + recent files; last-touched session preview with resume; installed skills inventory; scheduled-jobs ticker; **a real readout of which models have keys vs. which don't.** Density should mirror the chrome above. |
| O-2 | **S2** | HeroWelcome tip line (`HeroWelcome.tsx:113-117`) | First-run tip teaches a hotkey to an empty dashboard. The conversation primitives go untaught. | Rotate 3-4 first-run tips: `⌘K palette · / commands · @ files · /computer for desktop control`. |
| O-3 | **S2** | No API-key management UI anywhere (`SettingsModal.tsx`, `ModelPicker.tsx:277-293`) | Operators with no env vars set hit a toast and a dead end. Slack gets an 8-step wizard with live verify; API keys get "edit `~/.freyja/.env` yourself." | Add an "api keys + credentials" section to SettingsModal. Read/write `~/.freyja/.env`. Mask + paste-and-verify. When ModelPicker opens with zero available models, surface an inline "add a key" shortcut. |
| O-4 | **S3** | Splash screen (`SplashScreen.tsx:37,56,563+`) | 14s with no on-screen skip hint. New users sit through the full duration the first time, every time. | After ~3s (post-icon-fade-in), fade in a low-contrast `press any key` hint at viewport bottom. Or cut default duration to 8-10s; full pearl available with `shift` on launch. |
| O-5 | **S3** | Title-bar wordmark (`TitleBar.tsx:82-91`) | The `freyja` wordmark uses a cyan→blue→purple→magenta gradient — the single most saturated element in the chrome, contradicting the otherwise-disciplined palette. | Either drop the gradient (fg-0 wordmark + accent topographic mark), or pull the gradient inside the accent family: `#7aafea → #a8d4fc → #c4e0fc`. Lose the magenta. |
| O-6 | **S3** | Splash dissolves to hero with no breath (`HeroWelcome.tsx:30-63`) | Icon expansion (1.8s) starts the moment splash dissolves; the cards and tip line **pop in** with no entrance. After 14s of choreographed motion, the supporting content arriving unannounced is jarring. | Stagger the bottom strip entrance after the icon settles. Cards fade-translate up with 120ms stagger; tip line resolves last. Same `easeOutCubic` for continuity. |
| O-7 | **S3** | Title-bar readouts all zero on first run (`TitleBar.tsx:179-190`) | `ctx n/a`, `spend $0.00`, sessionId placeholder. Instruments out, all reading null. Visual weight without value. | Collapse numeric readouts into a single "awaiting first turn" kicker until the first turn lands. Reveal full instrumentation post-first-message — make the title bar switch from "standby" to "live" visibly. |
| O-8 | **S3** | Permission wizard step 0 double-dismiss (`ComputerPermissionWizard.tsx:82-87, 138-143`) | Header "skip" + step-0 footer "not now" within 200px. Same destructive action twice. | Keep header X. Drop "not now" from step-0 footer; only show "next". |
| O-9 | **S3** | ModelPicker footer hint (`ModelPicker.tsx:411`) | "⏎ preview" — Enter selects+closes, doesn't preview. Wrong verb. | "⏎ use · effort chip selects · esc cancel". |
| O-10 | **S3** | BridgeStatus pill (`TitleBar.tsx:215-232`) | Offline / demo mode surfaced only as a 5-letter pill with explanation in a hover tooltip. Easy to miss. | When `mode != 'live'`, render an unobtrusive single-line banner under the title bar with `modeDetail` inline (no tooltip required). |
| O-11 | **S3** | FALLBACK_MODELS vs bridge models (`ModelPicker.tsx:11-38`) | Hardcoded fallback list shows future-tense model names (Opus 4.8, GPT-5.5) before the bridge reconciles. Briefly displays fictional models on first-run. | Gate fallback rendering on `bridge mode === offline` explicitly, or mark fallback-rendered rows as `catalog (offline)` until reconciled. |
| O-12 | **S3** | Reduced-motion only affects CSS fade (`globals.css:1039-1045`) | 13-second WebGL shader still runs in full for users who asked for less motion. | In SplashScreen.tsx, check prefers-reduced-motion at mount; if true, skip the splash or render a 600ms static iridescent frame. |
| O-13 | **S4** | HeroWelcome workspace fallback (`HeroWelcome.tsx:95-99`) | `"detecting workspace..."` has no follow-up if detection fails. Ellipsis stays forever. | After ~2s with no resolution: `"no workspace detected · pick one"` with a click target. |
| O-14 | **S4** | Settings yolo label (`SettingsModal.tsx:53`) | `"Auto-approve everything -- yolo"` — double-dash artifact. | Real em-dash, or `"Auto-approve everything (yolo)"`. |
| O-15 | **S4** | Strategy toggle locked state (`TitleBar.tsx:152-154`) | Locks after first message; only cue is `opacity: 0.56`. | Render a small lock glyph or change kicker to `strategy · locked`. Visible state, not hover-discoverable. |
| O-16 | polish | Type size sprawl | ~12 distinct sizes between 8.5px and 15px; several 0.5px apart. | Consolidate to a 6-7 step scale (9 / 10 / 11 / 12 / 12.5 / 14 / 15). |
| O-17 | polish | Hardcoded ICON_SIZE (`SplashScreen.tsx:61`, `HeroWelcome.tsx:79`) | `190` duplicated; the comment "matches the hero-welcome mark exactly" is load-bearing but the constant isn't. | Export `ICON_SIZE` from a shared constants file. Author's note becomes enforced contract. |

##### Surprises

- **Delight**: the splash's two-channel gravity-well release (substrateGp releases at the snap, iconGp *does not* release because rings would re-expand during fade-out and produce a flash-back). Physical-correctness reasoning as a code comment — most splash screens are CSS keyframes.
- **Delight**: triple-Esc emergency stop with an explanation in source about why bare-Esc was removed (the agent's own `press_key('escape')` would self-cancel the in-flight tool call). Operator-empathy as source.
- **Delight**: the macOS-will-silently-privacy-filter warning in ComputerPermissionWizard step 1. No other agentic tool surfaces this.
- **Delight**: hardcoded FALLBACK_MODELS so the picker never blanks during bridge boot.
- **Friction**: the SettingsModal/SlackWizard asymmetry. Slack gets an 8-step wizard with live verify; API keys get "edit `~/.freyja/.env` yourself."
- **Friction**: the splash's 14s with no on-screen skip — the author put the skip in code but didn't surface it.
- **Friction**: the wordmark gradient is the only place the chrome breaks its own color discipline.

---

#### Surface 2 — Active session viewer

**Files inspected**: `Conversation.tsx` (1685 lines), `SessionPanes.tsx`, `InputDock.tsx`, `ToolCallChip.tsx`, `ParallelToolGroup.tsx`, `ActivityPanel.tsx`, `StickyHeader.tsx`, `ChildSessionBreadcrumb.tsx`, `DrafterActivityStrip.tsx`, `SkillCandidatesPanel.tsx`, `Sidebar.tsx`, `SubagentCard.tsx`, `Toast.tsx`, `MessageContextMenu.tsx`, `Widget.tsx`, `TopoBackdrop.tsx`, `lib/spinner.tsx`, `globals.css`, `tailwind.config.cjs`.

##### Pass 1 — Vibe

Adjectives: **instrumented · atmospheric · hand-built · monospaced · operator-coded · frosted · dense-but-disciplined · narratively-aware.**

- **Who made this?** A single author who lives inside the app. Both CSS and JSX comments explain *why* a choice was made ("reads as a stage direction in the screenplay sense"; "verifier byline. Sits where a co-author credit would sit on a print piece"). Files cite each other ("paired-peak height field, same vocabulary as Sidebar, different seed").
- **Era**: 2024–2025 modern brutalist terminal. Hybrid of Linear's restraint, Raycast's monospace bias, and the Apple-vibrancy-glass moment. The serif Fraunces escape hatch + named CSS animations push it past devtool-clone into something authored.
- **Human presence**: strong throughout. The kanban narrator copy ("reclaiming card_042 — heartbeat stale 4m", "judge rejected card_017") is uncommonly humane for an agent harness.
- **Screenshot-for-inspiration**: yes.

**Pass-1 verdict**: *This is what the brief promised. It feels made by a person who runs agents for a living and was tired of the generic chat-UI hand-wave. The vibe holds — the question is whether functional polish keeps pace with the atmospheric ambition.*

##### Pass 2 — Eye-path

**First focal point**: the streaming assistant message — character-reveal at 72 cps + blinking caret on active thinking blocks. The topo backdrop is pulled to `z-index: -10`; the middle column dominates.
**Intended focal point**: same. The activity rail and sidebar are deliberately quieter (10px uppercase grey labels, no glow). Confirmed by the `rounded-[18px]` glass capsules creating a frosted picture-frame.

**Hierarchy shape**: **podium** (not staircase). The streaming text, the running tool chips' accent spinner, the activity panel's gradient context meter, and `DrafterActivityStrip` (rail-top) all want attention simultaneously.

**Primary actions findable in <2s**: yes for cancel (red `■ force cancel (esc)` pill replaces workspace/model readout during streaming). **No visible "send" button at rest** — Enter is the only send affordance. Operator-coded but discoverability lives in the placeholder string.

Across-strip hierarchy (sidebar text-fg-1 / stream text-fg-0 / rail text-fg-2) bands the eye naturally toward the middle — correct. The friction is **section ordering inside the rail**: `DrafterActivityStrip` + `SkillCandidatesPanel` sit *above* the context meter and ToolTimeline. The most operationally-load-bearing content lives below two governance widgets. Priority-2 ordering issue, not a hierarchy break.

##### Pass 3 — Functional walkthrough

**Empty states**:

| Context | Designed | Note |
|---|---|---|
| HeroWelcome (no messages) | ✓ | See onboarding surface. |
| **Empty pane in split view** (`SessionPanes.tsx:177-186`) | ✗ | Centered 12px grey *"No messages yet."* / *"Session state is not loaded yet."* — no glyph, no affordance, no parity with HeroWelcome's care. The split pane is *exactly* when an operator needs the most context. Currently it's a label. |
| DrafterActivityStrip idle | ✓ | `"drafter · idle · awaiting first review pass"` — honest, terse, operator-voiced. |
| Diagnostics collapsed (`ActivityPanel.tsx:256-260`) | ✓ | `"12 events · 47 logs"` — informative summary, expandable. |
| ToolCallChip with no args yet (`ToolCallChip.tsx:39-46`) | ✓ | `summarizePartialJson` scrapes mid-stream args so the header isn't blank. Operator-built. |
| Subagent card with no result yet | ✓ | Spinner + label + task line + stats row. Solid. |
| Skill candidates empty | ✓ | "No pending skill candidates. The drafter writes here when a session produces a save-worthy generalization." Strong. |

**Loading states**:

| Context | Designed | Note |
|---|---|---|
| Streaming assistant text (`Conversation.tsx:1459-1545`) | ✓ | `useCharacterReveal` at 72 cps with rAF + carry. Gives the agent "a voice that types." Authored. |
| Thinking block streaming (`Conversation.tsx:1425-1454`) | ✓ | `rain` spinner + caret blink + glass-raised. Distinct from text parts. |
| Running tool call (`ToolCallChip.tsx:72-80`) | ✓ | Braille spinner + auto-expand args panel while streaming. Operator can watch the JSON build. |
| Parallel tool group lanes (`ParallelToolGroup.tsx:218-228`) | ✓ | 60%-width animated pulse bar per running lane + duration-comparison bars for completed lanes. Gantt-ish. **Surface that most clearly says "operator instrument."** |
| Widget loading (`Widget.tsx:168-189`) | ✓ | Cycling label + shimmer bar in 64px height. Calm. |
| Inline judge verdict pending (`Conversation.tsx:1267-1275`) | ✓ | `"judge thinking…"` placeholder while eventId resolves. |
| **Long-running turn with no streaming text** | ✗ | If a tool takes 90 seconds with no text, the only liveness signal is the braille spinner inside the chip + the title-bar dot. **There is no top-of-pane heartbeat / pulse / elapsed counter.** |

**Error states**:

| Context | Designed | Note |
|---|---|---|
| Tool call errored (`ToolCallChip.tsx:181-194`) | ✓ | Danger dot + `result (error)` label + danger-tinted pre. |
| **Refusal detected** (`Conversation.tsx:1217-1249`) | ✓ | `InlineRefusal` — danger ring + `refused` + category + model label. **Excellent — refusals as a real operational event, not a status code.** |
| Judge crashed (`Conversation.tsx:876-879`) | ✓ (partial) | Rendered as narrator line, but in identical italic fg-3 as `autopilot on` — visually equal weight to a low-stakes toggle. |
| Diagnostics auto-open (`ActivityPanel.tsx:86-88`) | ✓ | Opens on `warning logs > 0`. No in-conversation toast — only seen if eye lands on the rail. |
| **Network / transport error / disconnect** | ✗ | No offline / disconnected / stream-broke surface visible in code. Send button has no offline disabled state. *Needs live verification.* |
| **Cancel turn completed** (`InputDock.tsx:504-511`) | ✗ | Force-cancel posts `cancel()`; receipt is silence + spinner stopping. No "cancelled at bash · 3 subagents terminated · 12.4s in." |
| Image attach blocked non-Gemini (`InputDock.tsx:264-276`) | ✓ | Toast `"Switch this session to a Gemini model to attach video."` — clear, named, actionable. |

**Streaming-state highlights**:

- Text part does **not** render a trailing caret — only thinking blocks do. The reveal animation IS the streaming indicator, which works but a brand-new operator may not realize streaming is happening if reveal speed outpaces token arrival on a fast model.
- Tool-call args stream into an auto-opened pre with `· streaming` label + summarizePartialJson — JSON builds in front of the operator. Very on-vibe.
- Widget code-streaming via `ChromelessLoader` — defers `srcdoc` until `isStreaming=false` to avoid mid-build iframe re-mounts. Correct.
- Sub-agent stats roll live but **digits don't animate** — no `tabular-nums` flash or digit-roll. Liveness signal lost.

**Microcopy smells**:

| Where | Current | Better |
|---|---|---|
| `InputDock.tsx:478-479` placeholder | "Type @ to mention files, / for commands, paste images to attach" | Keep — operator-honest. |
| `SessionPanes.tsx:444` PaneChatbox placeholder | "Reply… / Send to this session…" | **Inconsistent with main InputDock's instrument tone.** "Reply…" is generic chat. Try "continue this turn…" / "inject into `<session-id>`…". |
| `SessionPanes.tsx:172` empty pane | "Session state is not loaded yet." | "hydrating `<session-id>`…" with a spinner, or an actual load button. Currently reads as a 404. |
| `InputDock.tsx:507` force-cancel tooltip | "Force-cancel this turn and every sub-agent running under it. Also bound to ⎋." | **Excellent. Keep.** |
| `MessageContextMenu.tsx:90-121` verbs | "edit · rewrite + rerun / rerun · replay verbatim / pin · keep through compactions / delete · rewind to here / branch · fork into new session" | **Excellent.** Second-line hints carry what-it-does, not just the verb. |
| `Conversation.tsx:824-883` narrator lines | "judge passed card_017 / reclaiming card_042 — heartbeat stale 4m / autopilot on" | **Excellent. Theatrical. Stays.** |
| `Conversation.tsx:816-818` InboxChip | "[click to expand]" | **Only un-operator-feeling cue in the surface.** Replace with a fg-4 ellipsis `…` + small `+N chars` counter, or a chevron. |
| `ActivityPanel.tsx:192-194` rail labels | "img trims / summaries" | Jargon. Use "images dropped" and "compactions" — match the underlying event subtypes (`media_pruning`, `compaction_complete`). |

**Stress findings**:

- **200-tool-call turn**: ParallelToolGroup groups by `groupId`, but a long *sequential* run renders as a wall of 32-40px pills. No collapse / "show last 10" / time-bucket affordance.
- **Massive code block**: `.md pre` uses `overflow-x: auto` — scrolls fine. But **no copy button, no language label, no syntax highlighting hookup visible in `renderMarkdown`**. Operators lose every comfort of a normal dev environment.
- **Long sub-agent label**: SubagentCard renders inside a `truncate` parent — should truncate cleanly. But the agentType badge sits before it in the same flex row, so on small panes the agent label is first to disappear.
- **Long tool result**: ToolCallChip clamps result `pre` to `max-h-[260px]`; ParallelToolGroup clamps to `max-h-[200px]`. **Inconsistent ceilings for the same content type.**
- **Cancel pressed mid-flight**: chips transition from "running" to whatever final status arrived. **There's no "cancelled" visual vocabulary distinct from "completed"/"errored."** Operator can't forensically tell which tool calls finished vs. aborted.
- **Compaction event mid-turn**: latestCompaction card in rail shows tokens_before → tokens_after (good). In the conversation stream, compaction is a "system" part rendered as the small warn-glyph chip — easy to miss in a 200-message scroll.
- **Context-pressure**: no proactive "85% of context full" callout. `ctxPct` is computed but the meter is the only signal. **It stays accent color across the entire 0-100% fill range** — no warn/danger transition.
- **Sub-agent spawn**: `SubagentCard` renders inline, but **there's no flash / drop-in / pulse when a new card appears.** The "Hey, look, a new subagent" moment is silent.

##### Pass 4 — Aesthetic discipline

- **13 distinct type sizes** between 8.5px and 18px. Every size has a clear semantic (8.5 = tabular minor stat; 9-10 = label/uppercase chrome; 11-11.5 = secondary; 12-12.5 = body; 13+ = h1-h2). Reads as deliberate scale, not drift — closer to a print-typography stack than a Tailwind shopping list.
- **Color inventory** matches the palette in chrome, with two leaks:
  - **Tool category colors in `ParallelToolGroup.tsx:299-326`**: `#6ba3d6` read / `#5bbb5b` write / `#e0a040` web / `#b080d0` shell / `#60c0c0` agent / `#d0a040` bus / `#b6f2ff` media. Seven hardcoded hexes outside the steel-blue-plus-greyscale thesis. The category colors carry *meaning* (they teach the operator what kind of tool fired) — but they re-introduce a saturated rainbow that the palette deliberately avoids.
  - **Computer-action overlay `#f5b640`** hardcoded inline at `SubagentCard.tsx:333-338`.
- **Border weights**: 0.5 / 1 / 1.2 / 2 / 3px — consistent. 3px lane bar in parallel-tool-group is the loudest.
- **Radii**: 9 distinct values + 4 `rounded-*` aliases. `rounded-[6px]` vs `rounded-md` are functionally identical and could collapse.
- **Shadow layers**: hairline insets / glow-accent / session-pane-active / cradle ladder / kanban pulse animations / title-control pressed-in. The kanban subsystem has the cleanest motion-tied shadows.
- **Grayscale test**: **passes**.
- **Token discipline**: **mixed**. Real and disciplined at the chrome/glass level; arbitrary at the type/spacing level.

##### Pass 5 — Density and breath

| Region | Density | Earned? | Note |
|---|---|---|---|
| Sidebar | balanced | ✓ | Multi-section accordion + token-prefix search + depth-indented session rows. Earned. |
| Sticky header above stream | **empty** | ✗ | `ChildSessionBreadcrumb` only renders when nested. **No persistent header showing "current session · model · turn-elapsed · cost-so-far" at root.** Eye has nowhere to land for "where am I, what's happening overall" without traversing into the rail. |
| Message stream — user message | balanced | ✓ | 76% max-width, accent-tinted bubble, right-aligned. Clean. |
| Message stream — assistant | balanced | ✓ | Label dot + "assistant" + parts spaced 2.5. The render-cached optimization that skips painting non-tail messages is sophisticated. |
| Inline tool chip area (single, sequential) | **cramped** | ✗ | 200-tool-call sequential turn = wall of 32-40px pills. The dispatcher tells a story; the chip log doesn't structure it. |
| Parallel tool group | balanced | ✓ | Junction lines + category bar + duration bars + collapse-per-lane. **Surface that most clearly says "operator instrument."** |
| Sub-agent inline card | balanced | ✓ | Agent type + label + task + 4-stat footer + computer-live preview + phase-chain children. Lots of info per pixel. |
| Input dock (cradle) | balanced | ✓ | The fade-in-on-focus hint row is a great density trick: empty when idle, populated when engaged. |
| **Activity rail — overall** | **cramped** | ✗ | **Nine vertically-stacked sections**, four with sticky headers (DrafterActivityStrip + SkillCandidatesPanel + ComputerLiveView + StickyHeader-context + TaskProgress + ToolTimeline + Changes + Artifacts + Diagnostics). Every signal present; operator must scroll to find any. The sticky-pill morph helps; the ordering doesn't. Spend/context belongs at the top; drafter/skill telemetry in a collapsed governance subgroup. |
| Activity rail — context meter sub-region | balanced | ✓ | Bar + 3-col grid + billed-input + spend + img-trims/summaries + last-summary panel. Well-arranged. |
| Thinking block | balanced | ✓ | Collapsible glass-raised card, `max-h-300` with internal scroll. |
| Inline goal verdict card | balanced | ✓ | Header row of pills + clamped reason + optional open-questions list when expanded. Dense but stratified. |

**Overall**: the center column is intentionally airy (`mb-6` between messages, generous padding) while the activity rail runs at print-density. The dichotomy is *correct* for a workshop instrument — BUT the rail runs at print-density with **nine top-level sections**, of which only three carry the high-load operational signal. **The rail needs editorial pruning, not more shrinking.** Stream is on-vibe; rail is over-stuffed; chrome-above-stream is under-stuffed.

##### Diagnostic — operator or customer?

**Score: operator.**

Every load-bearing primitive — character reveal, partial-JSON args header, narrator stage directions, file-change badge on chips, verifier byline on subagent cards, force-cancel pill with esc binding, in-conversation refusal card with category+model, parallel tool group with duration-comparison bars, sticky-pill section headers — is designed for someone who *lives in the app* and needs the agent's interior life made legible.

The customer-side drifts are tactical (no top-of-conversation chrome strip; rail sections in wrong order; no copy button on code blocks; tool category rainbow leaks the palette thesis) rather than aesthetic-soul drifts. **The base is right.**

##### References

- Linear (restraint, mono-leaning sidebar, palette discipline) — positive
- Raycast (monospaced command surface, fade-in-on-focus hints, `/`+`@` palettes) — positive
- Warp terminal (blocks-as-cards for command output; ToolCallChip + ParallelToolGroup ride the same idea) — positive
- Claude Desktop Imagine widgets (Widget.tsx explicitly cites — chromeless iframes float in stream) — positive
- Slate / Cursor agent sessions (SubagentCard's clickable "attach" with stats; child-session breadcrumb; force-talk to non-active session) — positive (file comments admit it)
- Charm / Bubble Tea TUIs (Departure Mono + braille spinners + uppercase tracked labels) — positive
- Helm dashboards / Datadog (rail's metric grids + sparkline-adjacent context bar + collapsible diagnostics) — positive
- **Generic SaaS chat** — *negative drift, only at `PaneChatbox` placeholder + empty-pane fallback*
- **Material Design / Atlassian Jira** — *negative drift, tool category color rainbow `#6ba3d6/#5bbb5b/#e0a040/#b080d0/#60c0c0/#d0a040/#b6f2ff`*

##### Punch list (severity-ordered)

| # | Sev | Surface | Finding | Fix |
|---|---|---|---|---|
| S-1 | **S2** | Conversation pane chrome | No persistent strip above the message stream showing session-title / model / turn-elapsed / live spend / cancel. Operator's load-bearing context only lives in the rail. When the rail is collapsed (focus mode), the operator is flying blind. | Add a 28-32px sticky header inside Conversation.tsx (above `ChildSessionBreadcrumb`, below the app title bar): status dot · session title · model · turn-elapsed (`tabular-nums`, updates every 250ms while streaming) · turn-cost · cancel pill (mirrors the cradle one). Use existing `.label` / chip vocabulary. When not streaming, collapse to 16px showing title + model only. *(file_ref: `Conversation.tsx:400-435`)* |
| S-2 | **S2** | Activity rail section order (`ActivityPanel.tsx:146-227`) | DrafterActivityStrip + SkillCandidatesPanel render at top, ahead of context meter + ToolTimeline + Changes. Governance shouts over operational. | Reorder: (1) ComputerLiveView when active, (2) context meter + spend, (3) TaskProgress, (4) ToolTimeline, (5) Changes, (6) Artifacts, (7) Drafter + SkillCandidates inside a collapsed "governance" supergroup, (8) Diagnostics. Top-down: what's NOW → what's coming → what just changed → what's the meta-loop saying. |
| S-3 | **S2** | Tool category color rainbow (`ParallelToolGroup.tsx:299-326`) | Hardcoded saturated category hexes contradict the explicit palette thesis. | Replace with either (a) accent-toned bar where saturation/opacity encodes category but hue stays in the accent/fg family, or (b) a category **glyph** instead of a color bar. Keep visual difference; drop the polychrome. |
| S-4 | **S2** | Long sequential tool-call runs (`Conversation.tsx:1017-1069`) | A turn that fires 100+ sequential tool calls renders as a wall of full-width glass-raised pills. No fold, no rollup, no time-bucketing. Stream becomes unreadable. | When a contiguous run of >12 sequential single-tool chips appears, auto-fold into a `tool burst · 47 calls · 38s` pill with chevron. Default expanded to last 5. Borrow visual language from the parallel-group header. **Exception**: tool calls with `frame` / `fileChangeSet` (load-bearing visuals) don't fold. |
| S-5 | **S2** | Cancel turn confirmation (`InputDock.tsx:504-511`) | Force-cancel posts `cancel()`; feedback is the spinner stopping. **Operator-feedback contract broken.** | On cancel, emit a one-line narrator entry into the stream: `turn cancelled — N tool calls in flight · M sub-agents terminated · Xs in`. Use existing italic fg-3 narrator treatment. Plus a Toast for at-a-glance confirmation when the conversation pane is scrolled. |
| S-6 | **S3** | Context-pressure tone escalation (`ActivityPanel.tsx:163-170`) | Meter stays accent across 0-100% fill. No warn at 80%, no danger at 95%. | Gradient stops: 0-70% accent, 70-90% warn-tinted, 90-100% danger. Plus a one-line callout above the meter when pct >= 85% (`context tight — next compaction at 95%`) styled like the latestCompaction card. |
| S-7 | **S3** | Code blocks in assistant prose (`globals.css:483-505`) | `.md pre` has bg/border/scrollbar — but **no copy button, no language label, no syntax highlighting** in `renderMarkdown`. | Add a 22px header row to `.md pre` in the conversation pane: language tag (left, label style) + copy button (right, accent on hover). Wire highlight.js or shiki into renderMarkdown. |
| S-8 | **S3** | Sub-agent spawn announcement (`SubagentCard.tsx:205-214`) | New card mounts via generic `.animate-fade-in` — no transient flash / pulse / glow announcing "a new agent just appeared." SubagentSwarmGrid (3+ parallel) doesn't telegraph either. | On first mount: 600ms `.kanban-drop-in` + 1400ms `.kanban-active-pulse` glow (both already defined in `tailwind.config.cjs:91-103`). Single subagent → soft glow; parallel swarm → coordinated drop-in stagger with 60ms delay between siblings. |
| S-9 | **S3** | Cancelled tool calls indistinguishable (`ToolCallChip.tsx:72-80`) | In-flight chips transition to whatever final status arrived. No `cancelled` state distinct from completed/errored. | Introduce 4th chip status: `cancelled` — hollow circle (vs filled), fg-3 color, italic `cancelled` duration label. Plumb cancelTurn() to mark in-flight ToolCallRecords. |
| S-10 | **S3** | Empty pane in split view (`SessionPanes.tsx:169-186`) | Centered 12px grey label — exactly the moment an operator needs the most context. | Bring HeroWelcome's vocabulary to PaneTranscript empty: agent type tag, sub-agent label/task, last-known status, switch-to-this-session button. If hydration is in-flight, show a braille spinner + `hydrating <session-id> · <bytes loaded>`. |
| S-11 | **S3** | Dead `AGENT_TYPE_COLORS` map (`SubagentCard.tsx:555-580`) | 13 agent types mapped to the *exact same* class string — vestigial scaffolding. Lying token. | Either delete the map + inline the constant, or re-activate per-agent tinting (verifier=warn, planner=accent) as a deliberate design move. Don't leave the dead Record. |
| S-12 | **S4** | PaneChatbox placeholder (`SessionPanes.tsx:444`) | `Reply…` / `Send to this session…` breaks tone with main InputDock's instrument voice. | Writable pane: `continue this turn…`. Non-writable pane: `inject into <session.title> · prefix with @ for context`. |
| S-13 | **S4** | InboxChip expand affordance (`Conversation.tsx:816-818`) | Literal `[click to expand]` — only visibly chat-app copy in the whole surface. | `…` ellipsis + `+N chars` counter inside `cursor:pointer` with tooltip. Or a chevron glyph. |
| S-14 | **S4** | Rail labels (`ActivityPanel.tsx:192-194`) | `img trims` / `summaries` jargon that doesn't match event subtypes. | `images dropped` and `compactions`. Tooltip surfaces the event subtype. |
| S-15 | **S4** | Tool result max-h inconsistency | ToolCallChip: 260px. ParallelToolGroup: 200px. Same content type. | Pick 260px, apply to both. Or shared constant. |
| S-16 | **S4** | Radii proliferation (`tailwind.config.cjs`) | 9 distinct values + 4 aliases that overlap. | Collapse to 5: 2px (mention) / 4px (kbd/badge) / 6px (chip/button) / 12px (card) / 18px (panel) + `rounded-full` for pills. Document in tailwind config comments. |
| S-17 | polish | Long-running turn liveness | If a slow tool emits no text for 60+ seconds, only the in-chip braille spinner signals life. | On the proposed top-of-conversation strip's status dot: low-opacity breathing dot (1Hz, accent) when `isStreaming === true` and no message has updated in >8s. |
| S-18 | polish | Subagent stats roll (`SubagentCard.tsx:247-251`) | Numbers update but digits don't animate. Liveness signal lost. | Wrap in `<span className='tabular-nums'>` + 120ms color-flash from accent → fg-1 on value change. Subtle. |
| S-19 | polish | Judge-crash narrator tone (`Conversation.tsx:735-746`) | `judge crashed on card_017` renders identical to `autopilot on` — operationally heavier event in same italic fg-3. | Override narrator color for `crash`/`blocked` subtypes to italic warn (or danger for `kanban_blocked`). Keep stage-direction treatment; lift the tone. |

##### Surprises

- **Delight**: `useCharacterReveal` (Conversation.tsx:1459-1545) is rAF-driven with a carry counter targeting 72 cps at 6 chars/frame cap. Explicitly handles fast catch-up vs. slow trickle, *and* disables on search query so highlights don't disappear under the typing cursor. More careful than most agent harnesses ship.
- **Delight**: `isHeartbeat` demotion in ToolCallChip — kanban heartbeats render as a 1-line muted background pulse instead of stacks of identical pills. The kind of decision only someone running long-lived agents would think to make.
- **Delight**: SkillCandidatesPanelContainer auto-expands when `pendingCount > 0` — "something arrived" nudge without a toast.
- **Delight**: TopoBackdrop is a hand-implemented marching-squares contour generator with hypsometric HSL tinting. Atmospheric labor that only pays off if you're committed to the bit.
- **Delight**: ChildSessionBreadcrumb uses the `scan` spinner explicitly — a continuous indicator on the breadcrumb even when the sub-agent isn't actively running. "You are inside another agent's session" is a *state*, not a loading.
- **Delight**: FloatingWidget treats `show_widget` tool calls as a special part-group kind and renders them chromelessly inline — the chip + parallel-group chrome would be "dead weight" (code comment's words).
- **Delight**: `PaneChatbox`'s `force` toggle — operator can interrupt another session mid-operation by arming a button that auto-resets after each send. No consumer agent UI ships this.
- **Friction**: the StickyHeader morphs between pill/section on scroll over 360ms — at native 60Hz smooth, but during busy scrolls with multiple sections sticking simultaneously the effect could read as fidget. *Needs live verification.*
- **Friction**: `.ascii` utility defined in globals.css but not used in any active-session components reviewed. Possible dead code from an earlier wordmark approach.

---

#### Cross-cutting themes

Three patterns show up across both surfaces:

1. **The chrome is operator-coded; the hero/empty states are customer-coded.** SplashScreen + HeroWelcome on first-run, and PaneTranscript empty in the session viewer, all fall back to centered-icon + soft-card geometry that looks like every other AI app. The instrumented surfaces (model picker, permission wizard, parallel tool group, refusal card, force-cancel) are uniformly excellent. The *idle* surfaces don't carry the same authorship.

2. **The system has a strong identity but weak token discipline.** Steel-blue accent, frosted glass ladder, mono digits, zero icon library, signature atmospheric assets — these are real and rare. But 1,137 arbitrary text sizes, 306 arbitrary spacings, 151 raw rgba()/rgb() literals, and `#a8d4fc` retyped 33× in components mean swapping the brand color would be a manual sweep, not a token change. **The aesthetic identity is unusually strong; the system is fragile.**

3. **Operator feedback contracts are inconsistent.** Cancel turn is silent. Long-running turns have no top-level pulse. Sub-agent spawn doesn't telegraph. Context-pressure doesn't escalate tone. Each of these is a moment where the instrument fails to confirm what the operator just caused, and the customer-feeling vacuum invites a comparison to chat apps that *also* never confirm.

---

#### Top-priority moves (combined)

In order of impact-per-effort, picking the items that compound:

1. **Replace HeroWelcome with an instrumented panel** (O-1). Biggest single change that moves the first-run diagnostic from `mixed_lean_operator` to `operator`.
2. **Add a persistent top-of-conversation chrome strip** (S-1) showing session · model · turn-elapsed · live spend · cancel. Without it, focus mode is flying blind. Pairs with item 8 below for long-running pulse.
3. **API-key management in SettingsModal** (O-3). Closes the most likely first-run dead-end. Required for the app to be self-installable.
4. **Activity rail section reorder** (S-2). Operational signal above governance signal. Cheap; high readability win.
5. **Long-sequential tool-call fold** (S-4). The current wall-of-pills is when the instrument feels least readable.
6. **Cancel turn receipt** (S-5). Silent destructive action is the most customer-feeling moment in the app right now.
7. **Token consolidation pass**: collapse type sizes to a 7-step scale, route all `rgba(168,212,252,*)` through `accent`, replace bespoke off-palette bg hexes (`#11111a`, `#15151b`, etc.) with `bg-1..bg-3`, document the radii hierarchy (2/4/6/12/18). Invisible polish, but compounds — every future component slots into a smaller system.
8. **Long-running-turn liveness** (S-17 + S-18). Breathing status dot in the new chrome strip when streaming idle >8s; `tabular-nums` digit-roll on subagent stats. Sells "instrument with a pulse."
9. **Replace tool category color rainbow** (S-3). Biggest single palette discipline leak in the codebase.
10. **Splash skip hint + reduced-motion respect** (O-4, O-12). Operators with reduced-motion shouldn't have to sit through 14s of WebGL.

Items 1-6 each move the diagnostic measurably. Items 7-10 are the polish that turns a hand-built tool into a hand-built tool *with system discipline.*

---

## Part 4 — Command center: the morning room

A forward-looking vision section. Not a review — a sketch of the surface that ties Freyja's accumulated infrastructure (scheduler daemon, multi-session, skill learning, multi-agent coordination, durable memory) into a single ritual surface. If Part 2's thesis is "an agent operator's workshop," this is where the workshop *opens for the day*.

### The thesis

> **A morning room. Reading the night's logs over coffee, then dispatching the day's work in five minutes.**

The operator's daily ritual shouldn't be "open Freyja → scan three tabs → context-switch to figure out where I was → start typing." It should be: **walk into the room, see what happened overnight, decide what runs today, hit go.** Then walk away.

This isn't a dashboard tab. It's a **scheduled, self-assembling, decisive view** that knows what every other Freyja surface holds, prioritizes it for the day, and stages real sessions ready to dispatch.

The framing matters: a *briefing* is what's delivered to a commander before action. Not a summary. Not a feed. A briefing **forces decisions**.

### Why this surface, and why now

Freyja has built the infrastructure to do this; it hasn't been composed:

| Asset | What it currently does | What the command center does with it |
|---|---|---|
| **Scheduler daemon** (`bridge/scheduler/daemon.py`) | Runs cron jobs; can spawn sessions | Fires a daily `morning-brief` session at the operator's wake time |
| **Persisted sessions** (`~/.freyja/sessions/*.{transcript,tasks,kanban,goal}.jsonl`) | Survive restart; carry full history | Brief reads them — "what completed overnight, what's stuck, what's blocked" |
| **Skill learning / drafter** | Watches sessions; generates candidate skills | Surfaces pending candidates as a section in the brief — accept / decline / edit inline |
| **Sub-agent profiles** | Specialized workers (explore, judge, code, verify, browser-qa) | Brief pre-stages the right profile for each priority item |
| **Kanban board** | Worker-coordinated card pipeline with judge review | Brief surfaces stale cards, blocked cards, judge-rejected cards |
| **Goal loop** | Same-session autonomous continuation with judge | Brief lists paused goals, completed goals, verdicts since yesterday |
| **Durable memory** (`~/.claude/projects/.../memory/`) | User/project/feedback/reference facts | Brief weights priorities against memory ("user is focused on X this week") |
| **Slack/Telegram gateway** | Out-of-app reach | Brief includes inbox-arrived items (mentions, scheduled replies) |
| **Tasks tool** (universal) | Per-session planning ledger | Brief promotes high-priority tasks to today's staged work |

None of these compose into a single morning surface today. The command center is the **composition**.

The strategic move: this is where Freyja stops being a tool you pick up and starts being a place you go. Once an operator has a morning ritual that runs without them, walking away from the desk becomes the feature, not the limitation. Async-first agent work needs an opening shot of the day — that's the command center.

### The diagnostic question for this surface

Same as everywhere else: *does this feel like a commander reading a morning briefing, or a customer reading a daily summary?*

A daily summary tells you what happened. A briefing tells you what to do. The customer version is Notion AI's "Good morning! Here's your day at a glance ☀️". The operator version is **a newsroom huddle board at 6:30am**: three columns of items, each with a verdict pending, each with a person assigned, each one decision away from being kicked into motion.

### Concrete layout

Vertical scroll, editorial-headered, four named sections. The header carries a Fraunces serif h1 (the existing escape hatch for hero typography) — *Tuesday, June 2 · 6 things since 22:14 yesterday.* Mono body. Each section is a self-contained band; the layout's tempo is *page* not *dashboard*.

```
┌─────────────────────────────────────────────────────────────┐
│  Tuesday, June 2 · 6 things since 22:14 yesterday           │  <- Fraunces h1, lowercase, light
│  ─────────────────────────────────                          │
│                                                             │
│  ▌ OVERNIGHT                                                │  <- .label, uppercase tracked
│    judge passed card_017 (ballot research v3)               │
│    explore-fast × 3 fanout completed · 47 sources · 32min   │
│    drafter saved 2 skill candidates                         │
│    kanban autopilot dispatched 4 cards, blocked 1           │
│    goal "freyja-design-pass" paused at iter 4               │
│                                                             │
│  ▌ NEEDS YOU                                                │
│    card_042 judge-rejected (iter 5/5) · BLOCKED             │
│      "complete coverage of city offices remains partial"    │
│      [ dispatch revision ]  [ override accept ]  [ defer ]  │
│                                                             │
│    drafter skill candidate: cooperative-compaction-helper   │
│      "save context budget on long research turns"           │
│      [ accept ]  [ edit ]  [ decline ]                      │
│                                                             │
│    goal "freyja-design-pass" wants you                      │
│      "the judge has 2 unresolved open questions"            │
│      [ resume ]  [ retire ]  [ rewrite goal ]               │
│                                                             │
│  ▌ TODAY                                                    │
│    1  finish ballot-research card_042 rework                │
│         worker: code · model: gpt-5.5 · ctx: 1M             │
│         pre-loaded with judge critique + prior artifacts    │
│         [ stage ]  [ run ]  [ skip ]                        │
│                                                             │
│    2  design pass on input-dock (per yesterday's review)    │
│         worker: browser-qa · model: claude-opus-4-8         │
│         attached: docs/DESIGN-REVIEW.md                     │
│         [ stage ]  [ run ]  [ skip ]                        │
│                                                             │
│    3  triage 4 stale kanban cards                           │
│         worker: judge-deep · model: claude-sonnet-4-6       │
│         coordination: kanban (current board)                │
│         [ stage ]  [ run ]  [ skip ]                        │
│                                                             │
│  ▌ PULSE                                                    │
│    yesterday: 4 sessions · $4.82 · 327k tokens out          │
│    longest run: card_017 (3h17min, autopilot)               │
│    most-used tool: web_fetch (213 calls)                    │
│    drafter watching: skill-learning, design-review-method   │
│    context discipline: 1 compaction · 0 forced              │
│                                                             │
│  ──────────────────────────                                 │
│   [ ✓ run 1, 2, 3 in parallel ]   [ run 1 first, then 2 ]   │
│   [ stage all and review later ]                            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

The geometry is *deliberately Things-3 / Linear-Today-shaped*, not dashboard-shaped. Each priority row carries:

- A short name
- The session shape Freyja's already chosen (model + coordination + sub-agent profile + pre-loaded context)
- Three actions: `stage` (assemble the session but don't run), `run` (assemble + dispatch), `skip` (drop from today)

The bottom strip is the **commit zone**: parallel dispatch, sequential dispatch, or stage-and-review. No "approve all" button — that's customer voice. Operator picks an actual plan.

### The triggering architecture

The brief is **itself a Freyja session**. This is the load-bearing decision.

1. **Cron**: scheduler daemon fires daily at the operator's chosen time (default 06:30 local, configurable). The fire is a regular durable scheduled job, same shape as anything `/schedule` creates today.

2. **Spawn**: the job spawns a session with `agent_type=morning-brief` — a new sub-agent profile (registered in `bridge/tools/agent_types.py`). The profile is read-only by design (no code edits, no `bash` writes), with broad reads (file system, kanban journal, task journals, goal state, skills, memory, session transcripts). Thinking: medium. Max iterations: 50. The brief is allowed to spawn `explore-fast` sub-agents for cheap parallel reads.

3. **Reading**: the brief session reads in parallel:
   - `~/.freyja/sessions/*.{transcript,tasks,kanban,goal}.jsonl` — overnight terminals
   - `~/.freyja/skills/{accepted,candidates}/` — drafter output
   - `~/.claude/projects/.../memory/` — durable facts
   - `~/.freyja/schedules/jobs/` — what's scheduled for today
   - Optional: calendar via MCP, Slack inbox via gateway
   - The current kanban board state
   - Active goal-loop state per session

4. **Producing**: the brief produces a structured artifact (`~/.freyja/briefs/2026-06-02.json`) using a JSON schema — sections + items + proposed session shapes. The structured shape lets the renderer apply the layout above without parsing prose.

5. **Emitting**: bridge emits a `brief_ready` event when the artifact lands. Renderer pre-empts the default view to the command center if the brief is from today and the operator hasn't seen it.

6. **Reviewing**: the operator scans, edits the staged sessions (re-pick model, attach context, rewrite the first message), declines items they don't want.

7. **Dispatching**: clicking `run` on an item (or the bottom commit zone) spawns the session(s) — new sessions for new work, reactivated sessions for resumed kanban cards or paused goals. Multiple parallel dispatch = multiple panes opened, all visible from the command center until the operator switches contexts.

8. **Returning**: the operator walks away. Sessions run. Re-opening Freyja shows the command center with a "since you left" status strip: which staged sessions completed, which need attention, which spent budget.

The brief itself is a session, so:
- It's re-runnable (`/brief refresh` or just rerun the morning-brief agent)
- It's reviewable (every input it considered is in its transcript)
- It's improvable (skills can sharpen its priority-ranking over weeks)
- It's introspectable (the operator can argue with the brief, in-conversation, and the next morning's brief carries that feedback via memory)

### Vibe principles specific to this surface

Six principles, each one a guardrail against drifting toward consumer-AI summary feed:

1. **Editorial, not algorithmic.** The header is a Fraunces serif sentence with a real date and a real count. *"6 things since 22:14 yesterday"* — not *"Your day at a glance ☀️"*. The brief reads like a memo a chief of staff would slide across a desk.

2. **Decisive verbs, not informational ones.** Every item answers *what to do*, not *what happened*. The verb buttons are the right scale: `dispatch revision` / `override accept` / `defer` / `accept` / `decline` / `stage` / `run` / `skip`. No "View details" or "Read more." If it's in the brief, it's actionable; if it's not actionable, it's pulse.

3. **Concrete items only.** The brief surfaces specific card ids, specific session ids, specific skill candidates. Never a paragraph that reads "you had a busy day yesterday." Specificity is the difference between "I read this" and "I trust this."

4. **No "approve all" button.** The commit zone offers real plans (`run 1, 2, 3 in parallel` / `run 1 first, then 2`), not a "yes to everything." The operator makes a real call. If the brief over-stages, the friction of declining items is the *signal that fixes the next morning's brief*.

5. **Self-explaining.** Each priority row carries its proposed session shape inline (model, coordination, sub-agent profile, attached context). The operator can see *why* this work would run that way before approving. No "trust me, I picked." If the brief picks the wrong model, the operator overrides inline; that override goes into memory; next time it picks correctly.

6. **Quiet when there's nothing to do.** A genuinely empty morning is honest: *"Quiet overnight. 0 sessions ran. Drafter has nothing new. Kanban backlog unchanged. Maybe a write/think day."* The brief never pads. Manufactured urgency is the loudest consumer signal in any AI product; the antidote is admitting calm.

The aesthetic vocabulary stays consistent with the active session viewer: Fraunces h1, `.label` uppercase section headers, mono body, hairline dividers, glass cards for items, accent dots for action verbs. **No new visual language** — the command center is built from the same parts as the rest of the app, just composed for ritual.

### What this surface explicitly is NOT

These are the failure modes; naming them up front prevents drift in implementation:

- **Not a Notion AI "Good morning, here's your day"**. No emoji. No greeting. The operator doesn't need to be welcomed; they live here.
- **Not a Datadog wall of metrics**. The pulse section is *one paragraph*, not a grid of sparklines. If the operator wants graphs, that's the mission dashboard's job.
- **Not a Slack inbox**. Notifications belong in toasts; commitments belong in the brief.
- **Not a feed that demands scrolling**. If the brief overflows above-the-fold, it's clustering wrong — collapse and link out.
- **Not a productivity tool with streaks, scores, "you've been productive today!" reinforcement**. The operator's relationship with the brief is professional, not gamified.
- **Not a calendar replacement**. The brief may *read* the calendar for context, but it doesn't try to be it.
- **Not a chatbot**. The operator doesn't talk to the brief — they read it, decide, dispatch. The brief that produced today's view is a session you *can* open and converse with (to argue priorities, ask why), but the morning room itself is *editorial output*, not a chat surface.

### Cross-section integrations that make this real

The command center is interesting only because it composes everything else. The integrations:

| Section | Integration | What changes elsewhere |
|---|---|---|
| OVERNIGHT | Reads session transcripts + kanban journal + goal state + scheduler job results | None — pure read |
| NEEDS YOU — judge rejections | Reads `kanban_judge_verdict` + `goal_paused` events | None — pure read |
| NEEDS YOU — skill candidates | Reads `~/.freyja/skills/candidates/`; inline accept writes to `~/.freyja/skills/accepted/` and removes from candidates | The existing SkillCandidatesPanel becomes a *secondary* surface — the command center is the primary review point. SkillCandidatesPanel can be moved into the rail's governance subgroup (already proposed in S-2 of the review). |
| NEEDS YOU — paused goals | Reads goal sidecar files | Goal-loop UI gains a "resume from brief" entry point |
| TODAY — staged sessions | Writes to `~/.freyja/briefs/<date>.json`; on dispatch, calls into the existing session-spawn machinery | New IPC command `brief.dispatch_item` that takes a brief id + item id, spawns the appropriate session. The session is born with the brief's pre-loaded context attached. |
| TODAY — proposed session shapes | Reads sub-agent profile registry; reads available models; reads installed skills | None — read-only against existing registries |
| PULSE | Reads usage / cost / tool stats from yesterday's session telemetry | None |
| Commit zone | Calls a new `brief.dispatch_plan(plan_shape)` IPC | Bridge gains a small dispatcher for plan shapes (parallel / sequential / stage-only) |

### Sub-agent profile sketch

A new agent type — registered alongside `judge-deep`, `explore`, etc.:

```yaml
morning-brief:
  description: |
    Daily briefing generator. Reads overnight session state, skill
    candidates, kanban activity, goal verdicts, and operator memory;
    produces a structured brief artifact for the command-center view.
    Read-only by design — no edits, no writes outside ~/.freyja/briefs/.
    Spawned by the scheduler daemon on the operator's morning cron.
  model: prefer_parent:parent -> claude-sonnet-4-6, gpt-5.5
  thinking: medium
  max_iterations: 50
  tools:
    - read_file
    - glob
    - grep
    - list_directory
    - list_skills
    - search_skills
    - load_skill
    - memory       # read durable facts
    - tasks        # read existing task ledgers (cannot create)
    - sub_agent    # only explore-fast children, for parallel reads
  output_schema: |
    {
      "since_when": "ISO timestamp",
      "sections": [
        { "kind": "overnight", "items": [...] },
        { "kind": "needs_you", "items": [...] },
        { "kind": "today", "items": [
          { "title": str,
            "rationale": str,
            "proposed_shape": {
              "agent_type": str,
              "model": str,
              "coordination": str,
              "attached_context": [str],
              "first_message": str (optional) }
          }
        ]},
        { "kind": "pulse", "summary": str, "stats": {...} }
      ]
    }
```

### What to build first (a real punch list, ordered by buildability)

1. **`agent_types.py` entry for `morning-brief`** with the spec above. Pure config; no UI yet. Operator can manually invoke via `sub_agent agent_type=morning-brief` to see the artifact land. *(1-2 hours)*
2. **`~/.freyja/briefs/` directory + JSON schema documented in `docs/`**. Brief sessions write here; renderer reads here. *(1 hour)*
3. **A renderer view at `/today`** that reads the latest brief from disk and renders the four sections in the layout sketched above. Static — no actions wired yet. This lets you see what a brief actually looks like before deciding which actions matter. *(half-day)*
4. **Scheduler integration**: a built-in scheduled job template (`/schedule add daily-brief 06:30 morning-brief`) that wakes the brief session. *(1-2 hours)*
5. **Inline actions on `NEEDS YOU` items**: accept-skill, defer-card, override-judge. These already have IPC paths in the bridge; the brief view just calls them. *(half-day)*
6. **`TODAY` dispatch wiring**: `brief.dispatch_item` and `brief.dispatch_plan` IPC. Spawns the right sessions with the right shapes. *(half-day)*
7. **Memory feedback loop**: every accept/decline/edit in the brief writes to durable memory so tomorrow's brief is better. *(1 day, including the prompt engineering for `morning-brief` to weight memory correctly)*
8. **The since-you-left strip**: when re-opening Freyja, the command center shows which staged sessions ran while you were away. *(quarter-day)*
9. **The polish pass**: editorial header, Fraunces h1, the action verbs above, the no-emoji rule, the quiet-morning case. *(quarter-day)*

Item 1-3 is enough to **see** what this surface feels like. Item 4 makes it run on its own. Items 5-6 close the loop. Items 7-9 are what separates this from being a clever feature versus being the surface that defines what Freyja is.

### What this unlocks (the second-order argument)

The morning command center isn't just another view — it changes the operator's relationship to the product.

- **Async-first becomes real.** Operator can leave the desk for hours and trust that something useful happened. The brief is the proof of that trust at the start of the next session.
- **The product compounds.** Skills accrue in `~/.freyja/skills/accepted/`. Memory accrues in `~/.claude/projects/.../memory/`. The brief gets sharper week-over-week because the *operator's feedback* is the training signal. Every accept/decline is a label.
- **Multi-session work becomes routable.** Today there's no good answer to "what should run in parallel?" — the operator picks ad hoc. The brief stages the parallelism so the operator approves it once and the dispatcher does the work.
- **The drafter's work becomes legible.** Skill candidates currently surface in the activity rail as a small accordion section that's easy to ignore. In the brief, they're a `NEEDS YOU` row with an accept button. That's the difference between a feature that's there and a feature that's used.
- **The product gets a daily ritual.** Almost no AI product has one. The few that do (Granola, Things, Sunsama) own their users' mornings. The morning command center is Freyja's claim on the same slot, with the difference that *something actually runs* when you commit.

### The thesis check

Does this make the user feel like an operator at a serious instrument, or a customer of an AI service?

If we build it as sketched — editorial header, decisive verbs, concrete items, real proposed session shapes, no "approve all," no "Good morning ☀️," honest about quiet mornings — it lands firmly on the operator side. It becomes the canonical answer to *"why do I use Freyja and not a generic agent chat?"*

If we build it as a daily summary panel with progress meters and "you've earned a 🎉" — it collapses into Notion AI's silhouette and we lose the differentiator we already have everywhere else.

The brief is the surface where the workshop metaphor either becomes the product's center of gravity or proves to have been just a pose. Worth building carefully.

---

### 2026-06-02 — Kanban + metrics + cross-cutting buttons/interactions

Three more deep-reads run in parallel after the first batch. Workflow ID `wf_c9f5f6f8-b6f`; raw structured returns at `/tmp/freyja-review-2.json`. The kanban and metrics reviews follow the 5-pass method; the third is a cross-cutting interactions audit with a different shape — entry-point inventory + diff-viewer-specific audit + hotkey table + consistency findings.

This batch was prompted by the user's specific complaint: *"there's no obvious way to get to the diff viewer (which itself has many types)."* The interactions audit found this is one of several features in the same shape — and that pattern is the most actionable theme of the batch.

---

#### Surface 3 — Kanban mode (KanbanBridgeView)

**Files inspected**: `KanbanBridgeView.tsx` (2,046 lines), `DispatcherBrief.tsx`, `JudgeBrief.tsx`, `AddTaskForm.tsx`, `MissionDashboard.tsx` (kanban path), `bridge/tools/kanban_board.py`, `bridge/freyja_bridge.py` (dispatcher events), `globals.css`, `tailwind.config.cjs`.

##### Pass 1 — Vibe

Adjectives: **instrumented · monochrome-with-spot-color · type-led · telemetry-dense · observational · judge-aware · static-policy-pane · underused-right-rail · novel-judge-meter · dispatcher-opaque.**

- **Who made this?** Someone who actually watched a judge fail-pass-fail-pass cycle and built a per-card iteration meter for themselves. The **JudgeIterationMeter + ConfidenceTrace** is workshop-grade — that's not a product manager's spec. But the DispatcherBrief is templated theater; the inconsistency suggests two passes by the same person at different sweat levels.
- **Era**: workshop-instrument-circa-2026. Type-led, mono everywhere, accent-on-restraint. Closer to a Datadog/Grafana operator console with serif headings than to Jira.
- **Human presence**: strong on the TicketCard and right rail. Faint to absent in the empty column slot, the DispatcherBrief policies, and any place the operator might want to *act* on a card. The maker watches their kanban but does not steer it from here.
- **Screenshot-for-inspiration**: yes.

**Pass-1 verdict**: *Operator-leaning, but with a hard ceiling: this is a beautiful observability surface, not yet an operator's workshop. You can read the loop; you cannot reach into it.*

##### Pass 2 — Eye-path

**First focal point**: the objective title (serif, 22px, top-left of SessionHeader).
**Intended focal point**: the board lanes — specifically `in progress` and `review` where dispatcher work is happening.
**Hierarchy shape**: **flat.**

Competing focal points:
- The stat-tile strip (10 tiles: agents / tasks / ready / in-flight / review / blocked / done / ctx / cost + autopilot toggle) all the same weight, same chrome — **a flat data wall** that pulls the eye sideways.
- Autopilot toggle (the most important header control) is *one of eleven equal-weight chips*.
- The agent roster in the right rail (warm avatars vs the rest of the grayscale).
- The animated `kanban-active-pulse` on running cards (intentional, good).

**Primary action findable in <2s**: **NO.** There IS no primary action on this surface — *that's the failure*. The kanban is observational. The only operator inputs are (a) the autopilot toggle in the header, (b) the `+ new` button on the READY column header, and (c) the DispatcherBrief. None of these have visual primacy.

##### Pass 3 — Functional walkthrough

| Context | Status | Note |
|---|---|---|
| Empty column slot | ✓ (generic) | Dashed border + `no {label}`. Fine for done/blocked. For RUNNING with autopilot ON + READY cards waiting, **this empty state should LIE about being empty** — it should say `next tick in 23s · will dispatch card_42` so the operator sees the autopilot's intent, not just its silence. Major source of dead-zone feeling. |
| No agents yet | ✓ (cheap) | Identical dashed-card treatment. Doesn't help an operator wondering why the dispatcher hasn't spawned one. |
| No activity yet | ✓ (cheap) | Same dashed-card. The activity feed could surface autopilot heartbeats (`idle tick · 14:32`) even when no real activity exists — currently the feed lies dormant until something interesting happens, which **makes a fresh session feel broken**. |
| Card spawning (dispatch → first heartbeat) | ✗ | Card transitions ready → flight with no spawning placeholder. The `kanban-drop-in` animates the card itself but there's no `worker booting` state. |
| Judge thinking | ✓ | The accent-pulsing segment inside the judge meter is the best loading state on the surface. **Keep this.** |
| Autopilot next tick | ✗ | Bridge ships `intervalSeconds` on every `kanban_tick` event explicitly so the renderer can render a countdown. **The renderer ignores it.** Autopilot is invisible between ticks. |
| **Worker crashed / timed_out / failed / cancelled** | ✗ | **CRITICAL.** Backend has statuses `crashed/timed_out/failed/cancelled`. `bucketCards` (`KanbanBridgeView.tsx:1905-1925`) routes all four into READY via the trailing `else`. A failed card visually re-enters the queue. The operator cannot distinguish a fresh ready card from a card that just blew up. |
| Card hit 5/5 rework cap (blocked) | ✓ (under-weighted) | `cap reached` label, warn color — but it's a small text label under the judge meter. A card that has consumed 5 judge cycles deserves its own visual treatment. |
| Stale card (kanban_stale fired) | ✗ | Bridge emits **per-card** stale events with `cardId`. The view only flags AGENT-stale (via `Station.stale`) — never card-stale. |
| AddTaskForm save failed | ✗ | `submit()` has no error handling for the IPC. If `addOperatorKanbanCard` rejects, button re-enables silently. **The operator's only authoring path is a silent-failure trap.** |
| TicketCard progress bar | ✓ (lying) | Progress percent is a **logistic-curve heuristic** from `startedAt` (`100 - 100/(1+min/6)`, capped 95%). The operator reads `47%` and has no way to know it's invented. ETA is the same — 8 minutes minus age. Both are fictional. |
| Judge verdict landing | ✓ | The kanban-verdict-pass / kanban-verdict-fail keyframes flash the segment for 900ms. The novel piece, well-engineered. |

##### Pass 4 — Aesthetic

- **Type sizes**: ~10 distinct sizes between 8.5px and 22px. Less sprawl than the active session viewer.
- **Colors**: agent-type palette adds 5-7 hues to the chrome (warmer than the rest of the app). Otherwise palette-disciplined.
- **Borders/radii/shadows**: kanban subsystem keyframes (`kanban-active-pulse`, `kanban-complete-glow`, `kanban-verdict-pass/fail`, `kanban-drop-in`) are the most disciplined motion vocabulary in the codebase — pure tokens.
- **Token discipline**: mostly system; ConfidenceTrace point colors are raw `rgb()` instead of `text-ok` / `text-warn` (small leak).
- **Grayscale test**: passes.

##### Pass 5 — Density

The chrome (stat strip + dispatcher brief) is **flat at print-density**; the right rail is **bottom-half empty**; the bottom strip (full-width 50px) shows 4 read-only stats with **no autopilot countdown / circuit-breaker / spend trajectory / burndown** despite the rail being prime real estate for live signal.

**Overall**: instrument is on for read, off for steer.

##### Diagnostic — `mixed_lean_operator`

The JudgeIterationMeter + ConfidenceTrace alone earns operator status — that's UI no AI service would ship because no one would understand it. The agent-type palette, the inline `kanban-active-pulse`, the stale-agent halo, the verdict flash, the per-iteration tooltip stack are all clear operator voice.

**But four customer-side traps drag it back**:

1. **The operator can only observe** — no manual dispatch, no force-block, no reassign, no unblock, no cancel-from-the-board.
2. **The DispatcherBrief is templated theater** — `edit` buttons have no `onClick`, the radio set has no state, the policies cannot be edited.
3. **The autopilot is invisible between ticks** despite the bridge emitting countdown data.
4. **Four backend error statuses silently bucket into READY** — hiding worker failure from the surface designed to make worker failure visible.

##### Utility findings — what an operator can't accomplish

The user explicitly emphasized utility / blank-space / understandability. Concrete user-tasks that fail:

1. **Cannot manually dispatch a single ready card when autopilot is off.** The only operator control is the global autopilot toggle. To start work on one card you flip a switch that *also* dispatches every other ready card on the board. **No surgical operator move.**
2. **Cannot cancel a runaway card from the UI.** A worker burning context in an infinite loop has to be killed via the agent's own session view, not from the kanban — the surface tracking the card cannot stop the card.
3. **Cannot unblock a card that hit 5/5 rework cap.** The `cap reached` label is purely informational; the card sits there forever. Unsticking requires CLI or session restart.
4. **Cannot see WHY a card failed without opening the worker session.** Status pip says `crashed` but the new view never shows the crashed status (silently bucketed into READY); even when it does, the operator must attach to the worker to read the trace.
5. **Cannot tell the dispatcher `never dispatch this card type` or `always assign this card to that agent type`** — DispatcherBrief *promises* these levers and they don't work.
6. **Cannot leave a note on a card.** Comments exist in the data model (`KanbanCardView.comments`) and are never rendered.
7. **Cannot see the autopilot's heartbeat.** Between `kanban_tick` events the surface is silent.
8. **Cannot tell at a glance which cards require verification vs which auto-seal.** `requiresVerification` flag is in the data, not in the UI.
9. **Cannot read the dispatcher's intent.** When the running column is empty, the surface says `no in progress` — but doesn't say `autopilot is on and will dispatch card_X to a fresh worker on the next tick`. The instrument exists; its narration is missing.
10. **Cannot scroll back through dispatcher history beyond the last 30 events.** For a multi-hour mission the surface forgets its own history quickly.

##### Punch list (kanban) — severity-ordered

| # | Sev | Surface | Finding | Fix |
|---|---|---|---|---|
| K-1 | **S1** | `bucketCards` (`KanbanBridgeView.tsx:1905-1925`) | Cards in `crashed/timed_out/failed/cancelled` fall into READY via the trailing `else`. Backend has 11 statuses; view handles 5. **A failed card is visually indistinguishable from a fresh one.** | Add explicit branches → blocked column with subtitle pips (`worker crashed` / `over budget` / `circuit breaker`), or a sixth lane `attention`. No silent fallthrough. |
| K-2 | **S1** | TicketCard / DetailDrawer | Operator has zero card-level actions: cannot cancel a runaway, unblock a stuck card, reassign, force-dispatch, kick a stale worker. The drawer footer only opens worker/judge sessions in split-view. | Add operator actions in the drawer footer: `cancel` / `unblock` (only when blocked) / `force dispatch` (only when ready+autopilot off) / `reassign type` (dropdown). Wire to bridge IPC (autopilot toggle proves the channel works). |
| K-3 | **S1** | DispatcherBrief | Every policy value, every who-can-claim rule, every escalation radio is hardcoded display text. The `edit` buttons on policy rows have no `onClick`. The radio set has no state. **The brief that's supposed to control autopilot cannot be edited.** | Either make it real — wire each policy to a bridge field — OR be honest and remove the edit buttons + add a banner explaining how policies are actually configured. Status quo is the worst of both: looks editable, isn't. |
| K-4 | **S2** | Card-level stale surfacing | Bridge emits `kanban_stale` with `cardId` after 90s of no card activity. View only surfaces AGENT-stale. A card with no heartbeat for 2+ minutes gets no warning on the card itself. | Card-level stale halo: amber border + `stale Nm` subtitle. Mirror the existing agent-stale halo. |
| K-5 | **S2** | Autopilot countdown | Bridge ships `intervalSeconds` on every `kanban_tick` specifically so the renderer can show a countdown. Renderer ignores it. | Countdown chip next to the AutopilotToggle: `next tick in 17s` (animated) when ON, `tick · paused` when OFF. Use the last `kanban_tick` event's `at + intervalSeconds * 1000` as the target. |
| K-6 | **S2** | Per-card manual dispatch | When autopilot is OFF, operator cannot start a single ready card without flipping the global toggle. | On READY-column cards (autopilot off), show a small `dispatch ↗` QuickLink in the card footer. Wire `kanban_force_dispatch_card` IPC. |
| K-7 | **S2** | TicketCard progress + ETA are fictional | `100 - 100/(1+min/6)` and 8-minute hardcoded ETA. Operator reads `47% · eta 4m 12s` and has no way to know it's invented. | Drop the numeric percent. Keep the flowing-highlight bar (conveys "work happening" honestly). Replace ETA with `last tool call 18s ago` or `first turn pending`. Fake numbers are the single most customer-shaped failure mode. |
| K-8 | **S2** | SessionHeader stat-tile strip flat hierarchy | 10 tiles, same weight, same chrome. Autopilot toggle reads as one of eleven equal chips. | Three groups with thin vertical rules: BOARD (5 tiles) · RESOURCES (3 tiles) · AUTOPILOT (toggle + brief, slightly larger or accent edge). |
| K-9 | **S2** | DetailDrawer comments missing | Card comments collected and normalized in `MissionDashboard.tsx:2892-2902`, never rendered in the new drawer. | Add a `comments` DrawerSection between brief and timeline, with an inline `add comment` input. |
| K-10 | **S2** | TicketCard missing verify-on-complete + retry pills | `requiresVerification` flag and `consecutiveFailures` count flow through `KanbanCardView` but the new view surfaces neither. | Port `KanbanVerifyOnCompletePill` and `KanbanRetryPill` onto the TicketCard chip row. |
| K-11 | **S3** | ActivityFeed | Sliced to last 30 events, no flash on new entries, no `N new since you scrolled`, idle ticks absent. | Accent flash on new rows (600ms), `N new` chip when not at latest, render idle ticks as gray heartbeat rows. |
| K-12 | **S3** | EmptyColumnSlot | Identical `no {label}` for all 5 columns. RUNNING+autopilot+ready-cards-waiting is *lying* — the autopilot WILL dispatch on the next tick. | Per-column empty copy that explains intent. RUNNING: `idle · next dispatch in Ns` / `paused · turn on autopilot`. REVIEW: `no cards awaiting judge`. DONE: `no completions yet`. |
| K-13 | **S3** | WipBar in BottomStrip | 4 colored segments with no legend, no tooltip, no labels. Cannot tell which color is which lane. | Title attrs per segment OR tiny color-keyed labels. OR drop the WipBar — header already shows the same numbers more readably. |
| K-14 | **S3** | JudgeIterationMeter segment click | Clicking the whole meter opens the *current* `judgeSessionId`. Each segment has its own `judgeSessionId` per iteration. | Make each `JudgeSegment` its own button, opening the per-iteration judge session. The novel piece undersells itself. |
| K-15 | **S3** | AddTaskForm error handling | `submit()` awaits with no try/catch on the success path. IPC rejection re-enables button silently. | Wrap in try/catch, warn-toned banner inside the modal with error + retry. |
| K-16 | **S3** | Right rail dead air | 320px rail, max-h-[42%] agents / rest activity. Session with 2 agents + 5 events leaves bottom half empty. | Third section: circuit-breaker status / next-tick countdown / spend trajectory sparkline. OR move BottomStrip into the rail and turn bottom strip into a real burndown. |
| K-17 | **S4** | ActivityFeed source provenance dropped | Bridge emits `source` on `kanban_tick` (`idle`/`post_turn`/`operator_create`); row reads it on the floor. | Append: `autopilot · idle` / `autopilot · post-turn` / `autopilot · forced`. |
| K-18 | **S4** | AddTaskForm keyboard shortcut | `+ new` is mouse-only. | ⌘N when no input focused → open form. Surface in column header tooltip. |
| K-19 | **S4** | DispatcherBrief counterfactuals | `if you reduce stale after to 8m...` describes edits the user cannot make. | Either make policies editable (K-3) or remove the counterfactuals. Counterfactuals on a read-only brief are gaslighting. |
| K-20 | polish | ConfidenceTrace point colors | Raw `rgb(168,176,168)` / `rgb(184,160,120)` instead of theme tokens. | Refer to `text-ok` / `text-warn` resolved at render time, or expose triplets in a constants file. |
| K-21 | polish | TicketCard mass mount animation | Every card drops in on first render. Initial load with 40+ cards = 1.2s cascade. | Skip drop-in when `card.createdAt < viewMountedAt`. Only animate genuinely new cards. |

##### Surprises (kanban)

- **Delight**: the JudgeIterationMeter is the strongest single piece of UI in the whole app — per-iteration segments + ConfidenceTrace sparkline overlaid in the same gauge. No AI tool ships this because no one understands it cold.
- **Delight**: `kanban-active-pulse` and `kanban-complete-glow` are different keyframes for `running` vs `freshly-done` cards. Subtle but operator-grade.
- **Friction**: the DispatcherBrief is the single most customer-coded surface in the app — placeholder UI that looks live and isn't. The contrast with the JudgeIterationMeter is the largest tonal break in the codebase.
- **Friction**: the kanban dispatcher is the most powerful piece of agent orchestration Freyja has built; the operator has no way to *adjust its behavior* from inside the app.

---

#### Surface 4 — Metrics dashboard + scheduled jobs

**Files inspected**: `MetricsDashboard.tsx` (2,966 lines), `ScheduledJobsDashboard.tsx` (676 lines), `MissionDashboard.tsx`, `ActivityPanel.tsx`, `bridge/freyja_bridge.py` (telemetry IPC), `globals.css`, `tailwind.config.cjs`.

##### Pass 1 — Vibe

Adjectives: **bespoke · operator-narrated · datadog-meets-letterpress · type-led · self-explaining-with-help · scheduler-orphaned.**

- **Who made this?** Someone who lives in the data — hand-rolled SVG charts, opinionated bespoke metrics ("cooperation", "thrash", "pressure band distribution"). MetricsDashboard is the most on-vibe surface in the app. The Scheduler dashboard reads like an unincorporated tenant.
- **Era**: Datadog/Grafana operator console crossed with editorial print. Departure Mono, hand-drawn charts, no off-the-shelf charting library.
- **Human presence**: strong in MetricsDashboard captions and tile philosophies. Near-zero in ScheduledJobsDashboard.
- **Screenshot-for-inspiration**: yes (for MetricsDashboard); no (for Scheduler).

**Pass-1 verdict**: *MetricsDashboard alone is a calling-card surface. Scheduler reads like a different author. The bigger structural problem is that the operator's economy lives in multiple places (compaction modal + scheduler tab + activity rail) and nothing pulls it into one instrument.*

##### Pass 2 — Eye-path

**First focal point**: the top-row stat tiles (turns / cost / cache / cooperation).
**Intended focal point**: ambiguous — the tiles ARE the first thing, but the cooperation tile (the most unique-to-Freyja metric) sits 6th in the row, indistinguishable from the others.
**Hierarchy shape**: **podium** — equal-weight tiles compete with each other.

##### Pass 3 — Functional walkthrough

| Context | Status | Note |
|---|---|---|
| Empty / first-day | ✗ | Individual panels say `no data yet` but there's no global onboarding: `Run a session for a few turns to start seeing your operator dashboard`. |
| Loading | ✗ (partial) | Manual `refresh` button shows `loading…` text but no spinner — inconsistent with InputDock's spinner vocabulary. |
| **Live updates** | ✗ | **Dashboard does NOT subscribe to the live telemetry stream.** Manual refresh only. Operator watching a session burn money in real time has to keep clicking `refresh`. The instrument is a freeze-frame, not a workbench. |
| Error states | ✗ | The IPC for telemetry can fail; the dashboard renders a flat empty state. No retry, no error surface. |
| Anomaly surfacing | ✗ | No `this session is 5× your baseline cost` callout. Operator has to spot it by eye. |
| Drill-through | ✗ | Clicking a session row opens a great detail drawer — but no way to JUMP into the session (open in workspace, view conversation, replay turn). Drawer is read-only. |
| Day-over-day comparison | ✗ | No `vs yesterday`, no `vs last week`, no baseline. Cannot tell if today is normal or anomalous. |
| Export | ✗ | No copy-as-CSV, no `reveal compaction.jsonl in Finder`. The bridge writes a beautiful JSONL log; the operator has no way to get to it without Terminal. |
| Session table truncation | ✗ | `SessionTable.slice(0, 80)` per group, no `show more`. Silent truncation. |

##### Utility findings — operator questions the metrics surface can/can't answer

| Q | Answer | Note |
|---|---|---|
| Which session burned the most tokens last week? | **Yes** | SessionTable sorts by spend; window=7d shows top. |
| Which session triggered the most compactions / thrash? | **Yes** | Compact column + thrash chip; filter `narrow=has_thrash`. |
| Is autopilot more cost-effective than solo turns? | **No** | No autopilot-vs-solo discriminator anywhere, even though `coordination_strategy` is tracked elsewhere. |
| Is one model failing more often than another? | **No** | Models shown by cost share + call count; **no failure rate per model.** `tool_call_metric` has an `ok` field that's never surfaced in the model panel. |
| How is my cache hit rate trending? | **No** | Current rate tile exists; no trend. |
| Which sub-agent profile is the busiest / most expensive / least successful? | **Yes** | Strongest operator lane in the surface. Per-profile table covers spawns, calls, spend, p95 latency, ok rate, budget. Clickable to drawer with outcome breakdown + top tools + recent tasks. |
| Did the agent cooperate or did the runtime have to step in? | **Yes** | Cooperation tile — but operator must know what "cooperation" means cold. No tooltip. |
| How much did I spend yesterday vs today? | **No** | No day-over-day comparison anywhere. |
| What was the most expensive turn this week, and what was it doing? | **Partial** | Cost-per-turn histogram shows distribution; bins aren't clickable. SessionDetailDrawer per-call table has per-call cost but only after the operator picks a session. |
| Which tool calls failed / were slowest? | **No** | `tool_call_metric` written but never aggregated globally. **Most operator-obvious miss in the surface.** |
| Which skills / memories were used most this session? | **No** | Not surfaced. |
| How often does the judge fail a drafter output? | **No** | Drafter/judge telemetry exists in `system_events`; never reaches this surface. |
| How much am I spending on the scheduler vs interactive use? | **Partial** | Scheduler tab shows 24h cost; MetricsDashboard shows total. **Operator must mentally subtract, in two different visual languages.** |
| Is there an anomaly I should look at? | **No** | No surfacing, no alerts, no baselines. |
| Let me export this | **No** | No path. |
| Let me jump from a session row INTO that session | **No** | Drawer is read-only. |
| Is my data live right now or stale? | **No** | No live stream, no auto-refresh indicator. |
| On my first day with Freyja, what should I do to populate this dashboard? | **No** | No global onboarding. |

##### Aesthetic + density

- **Type sizes**: ~12 distinct, deliberate scale.
- **Token discipline**: **mixed** — MetricsDashboard respects palette; **ScheduledJobsDashboard ships its own `.sjd-*` CSS as an inline `<style>` block** with sans-serif headers, blue submit buttons, 3px radii, rgba literals. Two designers, one product.
- **Grayscale test**: passes (in MetricsDashboard); marginal in Scheduler.
- **Density**: MetricsDashboard balanced-to-cramped; Scheduler **cramped-but-empty** (9 sjd-kpi cards, one unlabelled sparkline, one bare top-jobs table — admin-page tempo).

##### Diagnostic — `mixed_lean_operator`

MetricsDashboard is the most on-vibe surface in the app — bespoke metrics, hand-rolled charts, opinionated. ScheduledJobsDashboard drags the score down — it reads like a port from another app. The bigger problem is **the agent-internal life of Freyja is invisible**: skills loaded, judge passes, drafter accept rate, autopilot effectiveness, tool failure rates — all written to disk, none reaches the dashboard.

##### Punch list (metrics) — severity-ordered

| # | Sev | Surface | Finding | Fix |
|---|---|---|---|---|
| M-1 | **S2** | MetricsDashboard header | Label `compaction metrics` undersells the panel — surface now hosts spend, cache, models, profiles, agent decisions, cooperation. Frames the instrument as narrower than it is. | Rename to `telemetry` or `instruments`; demote `compaction metrics` to a subtitle. |
| M-2 | **S2** | ScheduledJobsDashboard visual language | Inline `<style>` block with sans-serif headers, blue submit buttons, 3px radii, rgba literals. Single biggest off-vibe surface in the app. | Port to shared design system: glass-raised cards, hairline rings, Departure Mono labels, mono numerals, BAND_COLOR / fg-* / accent palette. Delete the inline STYLES block. |
| M-3 | **S2** | No tool-call telemetry panel | `tool_call_metric` rows are tracked diligently (call count, duration_ms, ok flag, result_bytes); only surfaced inside ProfileDetailDrawer's "top tools". **No global error-rate panel, no total result-bytes, no p95-per-tool.** The richest tool data in the app is hidden. | Add a top-level `tools` panel: ranked bars by call count, error rate column, p95 duration, total result bytes. |
| M-4 | **S2** | Live updates | Dashboard does NOT subscribe to the live telemetry stream — manual refresh button only. | Subscribe to `system_events` / telemetry stream; auto-refresh toggle; visible `live` indicator. |
| M-5 | **S2** | Day-over-day comparison | No `vs yesterday`, no `vs last week`, no baseline. | Delta indicator next to every stat tile (`total spend $4.21 ↑12% vs prev 7d`). Baseline line on SpendChart. |
| M-6 | **S2** | No drill-through | Clicking a session row opens a great drawer but cannot JUMP into the session itself. Drawer is read-only. | `open session` button in drawer header that calls `openSessionPane(id)` and closes the modal. Same for Profile drawer's session list. |
| M-7 | **S2** | Agent-internal economy invisible | Skills / memory / drafter / judge / autopilot / kanban dispatcher all emit `system_events`; none show up here. | Extend telemetry IPC to read per-session `system_event` stream. Add small panels: skill activations, judge pass rate, drafter accept rate, autopilot effectiveness. |
| M-8 | **S3** | Cooperation tile opaque | Most bespoke and operator-meaningful tile in the dashboard; label is opaque cold; no tooltip. | `title=` attribute: `Share of compactions the agent triggered itself (40-80% band) vs runtime fallback (>=80%). Higher agent share = healthier cooperative protocol.` |
| M-9 | **S3** | SpendChart underdense | 800×160 chart with one path, three gridlines, just start/end date labels. | y-axis ticks at $1/$5/$10; hover crosshair; peak-rate callout; per-day stripe shading. Consider per-day bars (operator can derive cumulative; per-day surfaces spike days). |
| M-10 | **S3** | Turn histogram clicks are dead | Clicking a bin does nothing. | Make bars clickable → filter the session table to sessions containing turns in that cost bin. |
| M-11 | **S3** | No export | No copy-as-CSV, no `reveal in Finder`. The bridge writes a beautiful JSONL log; operator has no way to it without Terminal. | Footer with `reveal compaction.jsonl in Finder`, `copy filtered rows as JSON`, `export per-session table as CSV`. |
| M-12 | **S3** | Session table silent truncation | `slice(0, 80)` with no `show more`. | `showing 80 of 142 — load more` or paginate. |
| M-13 | **S3** | Pressure band caption | Ends at `fallback 80–95%` without explaining what `fallback` means. | Extend: `fallback 80–95% — runtime emergency stop; means the agent didn't compact cooperatively`. |
| M-14 | **S3** | Filter bar visual collision | Four segmented filter groups, uppercase 9.5px labels compete with the tiles below. | Collapse into a single `filters` popover chip, or shrink the row's vertical padding. |
| M-15 | **S3** | Model name humanization | Names like `sonnet-4-20250514` get `.replace('claude-', '')` but no further humanization. | Centralize a `modelLabel()` helper. |
| M-16 | **S3** | Keyboard navigation absent | Session + Profile tables are mouse-only. | j/k or arrow keys; Enter to open; ⌘F to focus quick-filter. |
| M-17 | **S3** | Scheduler Metrics tab underbuilt | 9 sjd-kpi cards, one unlabelled sparkline, one bare top-jobs table. | After porting to shared design system, add: spend-per-day-by-job stacked area, failure-mode histogram, p95 run duration per job, next-24h scheduled runs preview. |
| M-18 | **S3** | Discoverability split | MetricsDashboard behind a title-bar button visible only at xl: breakpoint. Scheduler is a tab inside MissionDashboard. **Operator has no single entry point to "how is my Freyja doing".** | Merge Scheduler into MetricsDashboard as a sub-view, OR make MetricsDashboard a tab inside MissionDashboard. Current split is a UX accident. |
| M-19 | **S4** | Scheduler cost formatting | `$0.0000` fixed 4-decimal in scheduler; `formatCost()` elsewhere. | Use shared `formatCost()`. |
| M-20 | **S4** | Last-fetched timestamp absolute | `refreshed 3:42:18 PM` — no relative time. | Use existing `relativeTime()` helper. |
| M-21 | polish | Color tokens for tones | Cache reuse, compactions, cooperation, budget can all turn `warn` (amber) simultaneously. Three amber tiles read as `three problems` when it might be one amplified. | Reserve `warn` for single most-actionable severity per row; introduce subtle `fyi` tone. |
| M-22 | polish | SpendChart axes locale | x-axis `toLocaleDateString` — locale-dependent, inconsistent with the mono aesthetic. | Stable `YYYY-MM-DD` or `Mar 14` in mono. |

##### Surprises (metrics)

- **Delight**: MetricsDashboard is the most on-vibe surface in the codebase. Bespoke metrics, hand-rolled SVG, opinionated.
- **Delight**: Cooperation tile is one of the most original metrics in any AI workbench. Agent-driven vs runtime-fallback compaction share, with mean pressure at each band.
- **Surprise**: `compaction.jsonl` is the ONLY data source the metrics IPC reads — but it contains `llm_call_metric` / `pressure_signal` / `summarize_context_call` / `profile_invocation` / `profile_completion` / `tool_call_metric`. **The "compaction" name is a vestige.**
- **Surprise**: `tool_call_metric` rows are tracked diligently (call count, duration_ms, ok flag, result_bytes); aggregated *only* inside ProfileDetailDrawer's "top tools". The richest tool data in the app is hidden.
- **Surprise**: ScheduledJobsDashboard ships its CSS as one big `<style>` string at the bottom of the file. Parallel design system inside the same React tree.
- **Surprise**: NO live event listener anywhere in the dashboard. Every other long-lived surface in Freyja at least polls. MetricsDashboard does neither.

---

#### Surface 5 — Cross-cutting buttons + interactions + diff-viewer audit

**Files inspected** (28): Conversation, InputDock, Sidebar, TitleBar, MissionDashboard, MetricsDashboard, KanbanBridgeView, CommandPalette, QuickSwitcher, SettingsModal, ModelPicker, MessageContextMenu, ToolCallChip, SubagentCard, ChildSessionBreadcrumb, BranchSessionDialog, SessionPanes, ConversationSearch, Toast, DebugDrawer, SkillCandidatesPanel, FileChangeCard, ChangesSection, ArtifactWorkspace, ArtifactPreview, ActivityView, plus `slash.ts` and `store.ts`.

##### Headline finding

The user said: *"there's no obvious way to get to the diff viewer (which itself has many types)."* **The audit found this is a SHAPE, not a single bug.** Six features sit in the same condition — rich functionality, unreachable from global UI:

| Feature | Reachable via | Discoverability |
|---|---|---|
| **Diff / Changes workspace** | 9px chip in ActivityPanel → ChangesSection (only path) | **HIDDEN** |
| **Artifact workspace** | 9px chip in ActivityPanel → ArtifactsSection (only path) | HIDDEN |
| **ScheduledJobsDashboard** | Mission Dashboard → "scheduler" tab (only path) | HIDDEN |
| **SubagentDetail full-screen overlay** | Command Palette → search "subagent" → pick (only path) | HIDDEN — **orphaned UI** |
| **QuickSwitcher (Ctrl+Tab MRU)** | Ctrl+Tab — never documented anywhere | HIDDEN |
| **DebugDrawer** | ⌘D — `/debug` is marked `hidden=true` so doesn't appear in autocomplete | HIDDEN |

Of 36 features audited: **18 well-reachable, 12 partially-reachable, 6 hidden.** A third of the product's surfaces have low or zero discoverability.

##### Diff viewer — focused audit

The user's specific complaint, fully decomposed.

**Components rendering diffs** (7):

- `FileChangeCard.tsx` — inline diff blocks with +/- coloring, expand-on-click
- `ChangesSection.tsx` — activity-rail of recent change sets, collapsible inline diffs, `jump` to tool call, `diff view ↗` to open workspace
- `ArtifactWorkspace.tsx` — full-screen workspace with four view modes; the `changes` view (lines 943-1102) is the dedicated diff browser
- `ToolCallChip.tsx:180` — embeds FileChangeCard in expanded chips with `fileChangeSet`
- `ParallelToolGroup.tsx:284` — same FileChangeCard embedding in parallel groups
- `ComputerLiveView.tsx` — live computer screenshot (no diff, just latest frame)
- `ToolCallChip.tsx:121-157` — single screenshot frame on a computer-use tool call (no before/after)

**Diff TYPES supported, with reach + discoverability**:

| Type | Where | How to reach | Discoverability |
|---|---|---|---|
| Per-file unified diff (+/-/@@) | `FileChangeCard.tsx:107-173` | Expand a write/edit ToolCallChip; OR ActivityPanel → Changes → expand row; OR ActivityPanel → `diff view ↗` → workspace | **medium** |
| File-change badge (count + +N / -N) | `FileChangeCard.tsx:13-22` | Always visible on tool chips with file changes | high |
| Change-set timeline (multi-file in one turn) | `ChangesSection.tsx:99-158` | ActivityPanel → Changes section. Shows 8 inline; rest via `diff view ↗`. Requires panel uncollapsed. | medium |
| **Cross-session diff workspace** | `ArtifactWorkspace.tsx:943-1102` (ChangesWorkspaceView) | ActivityPanel → Changes → `diff view ↗`. OR ActivityPanel → Artifacts → `workspace ↗` → ⌘4. **No global hotkey, slash, palette, title bar button.** | **LOW** |
| Single computer-use frame | `ToolCallChip.tsx:121-157` | Expand computer-use chip in conversation | high |
| **Computer screenshot delta / before-after** | (does not exist) | Not reachable. **No visual diff between consecutive computer-use frames**, no overlay of planned-vs-actual click target. | **HIDDEN — DOES NOT EXIST** |
| **Artifact text diff (subagent rev N vs N-1)** | (does not exist) | Only flat preview via ArtifactPreview. **No `previous version` or `compare with` affordance.** | **HIDDEN — DOES NOT EXIST** |
| Tool result image array | `ToolCallChip.tsx:207-231` | Tool call result expansion | medium (no diff) |

**What works**:

- Inline diff in tool chips is unmissable when expanded — strong default discoverability.
- `FileChangeBadge` is reused consistently across tool chips and change-set rows, so the operator learns the `+N/-N` idiom fast.
- `jump` button on each change set bounces back to the originating tool call — well-wired cross-link.
- ArtifactWorkspace has its own ⌘1/2/3/4 view-mode keys + ⌘F search.

**Gaps**:

- **Zero global entry points to the diff workspace itself.** If ActivityPanel is collapsed (⌘]), the diff view is functionally unreachable.
- No `/diff` or `/changes` slash command. `/export` exists but not the natural inverse.
- Command Palette has `Mission Dashboard / Findings / Swarm Monitor` entries but **no `Changes Workspace`** or `Artifacts Workspace`.
- Workspace state lives in component-level `useState`, not in the store — no programmatic way to deep-link to a specific diff view.
- ArtifactWorkspace's ⌘1-4 / ⌘F hotkeys documented only in tooltips *inside the modal*.
- No before/after view for computer-use sessions. Operators reviewing a botched UI automation can't see *what changed on the screen* between adjacent actions.
- No artifact versioning. If a subagent edits the same `.md` three times, the operator sees three flat previews — no chronological diff.
- View mode is called `changes` in the workspace toggle (icon ±) but the section header calls it `diff view` and the inline chip says `diff`. **Three names for the same concept.**

**Verdict**: *The diff functionality is rich (per-file unified diffs, workspace browser, transcript jump-back) but its existence is essentially unsignaled outside the activity-panel rail. Naming is inconsistent. The workspace is buried two clicks deep behind a 9px-uppercase chip whose location is dependent on the activity panel being visible. No command palette / slash / hotkey path exists. Computer-use sessions and artifact versions get no diff view at all.*

##### Hotkey inventory

13 documented · 11 undocumented · 4 conflicts.

**Documented** (visible in tooltips, footers, or slash help):
⌘K palette · ⌘⇧M dashboard · ⌘, settings · ⌘N new session · ⌘O swarm · ⌘[ sidebar · ⌘] activity rail · ⌘\ focus mode · ⌘Esc cancel turn · ⌘⇧Esc emergency stop computer · Triple-Esc emergency stop · ⌘F in-conversation search · Esc close modal.

**Undocumented** (work, but operators must discover):

- **Ctrl+Tab / Ctrl+Shift+Tab** — quick switcher MRU. Only documented *inside* the switcher when it's already open.
- **⌘B** — "go to parent session". `slash.ts:34` still says `/burst (Cmd+B)` — comment at `App.tsx:407-409` explains semantics changed; **slash help is wrong**.
- **⌘D** — toggle debug drawer. `/debug` is `hidden=true` in slash; only appears via manual entry.
- **⌘1 / ⌘2 / ⌘3 / ⌘4** — switch ArtifactWorkspace views. Active only when workspace is open.
- **⌘F (inside ArtifactWorkspace)** — focus workspace search. **Conflicts with Conversation's ⌘F.**
- **Enter / Space on Kanban card** — open detail. Discoverable only because `role='button' tabIndex=0`.
- **j / k inside SwarmMonitor** — vim-style row navigation. **Not advertised; used nowhere else** in the app.
- **⌘↵ inside message edit textarea** — save + rerun. Surfaced as a footer hint mid-edit.
- **Tab / Enter** inside InputDock slash + @file + ~/ path popups — visible only via the popup itself.
- **Esc** inside slash/file/path popup — dismiss without losing typed text (convention, unsurfaced).

**Conflicts**:

- **⌘F** bound in both `Conversation.tsx:187` and `ArtifactWorkspace.tsx:99-103` (both global window listeners). When workspace is open over conversation, both fire; only call-order saves correctness.
- **Esc** overloaded across many handlers: triple-Esc emergency stop + bare Esc dismiss + Esc in ArtifactWorkspace + Sidebar inspector + MessageContextMenu + MetricsDashboard. The triple-Esc detector reads keys *before* dismiss handlers, but fast dialog-close sequences could accidentally arm the panic counter. Known footgun acknowledged in `App.tsx` comments.
- **⌘B** documented as `/burst` in `slash.ts:34`; binding now opens parent session. Slash help is stale.
- **⌘O** exposed as `/subagents` but action calls `toggleMissionDashboard(true, 'overview')` — there's **no subagents tab**. Label and behavior diverge.

**Coverage notes**: top-level layout shortcuts (⌘[ ⌘] ⌘\ ⌘K ⌘,) are good. Session navigation has a strong Ctrl+Tab MRU but invisible to new users. **No hotkey to:** cycle between panes (split view), scroll between tool-call chips, focus the InputDock, expand/collapse swarm cards, open the diff or artifact workspace. **Power-user keys (j/k vim in SwarmMonitor) exist nowhere else.**

##### Context menu coverage

**Present**: Conversation messages (right-click → MessageContextMenu); Sidebar session rows (right-click + hover ⋯).

**Missing**:

- Skills rows (Sidebar)
- Memory rows (Sidebar)
- Sub-agent rows (Sidebar swarm)
- **Tool call chips** (no `copy args` / `copy result` / `rerun tool` / `jump to file` / `open in diff workspace`)
- **File-change rows** in ChangesSection (no `open in editor` / `copy path` / `revert` / `stage`)
- **Artifact cards** in ArtifactWorkspace
- **Kanban cards** (only Enter/Space to open — no `reassign` / `duplicate` / `change priority` / `move to column`)
- Bus-flow nodes
- Computer live view screenshot
- Subagent cards (`SubagentCard` is one big click-to-attach)
- ToolTimeline gantt rows

**Inconsistencies**:

- Sidebar session rows have BOTH right-click AND hover ⋯ button. Conversation messages have only right-click — no ⋯. Operators trained by Sidebar will look for ⋯ on every row.
- `MessageContextMenu` uses a portal at `z-[60]`; Sidebar inline context menu renders inline at `z-30` — gets clipped by `overflow:hidden` ancestors. **Different implementations of the same primitive.**

##### Inconsistent affordances (named)

1. **Session row open semantics differ** by location. Sidebar: click=replace, ⌘-click=split. Sidebar swarm row: same. Command Palette: always replace. Right-click context: explicit options.
2. **Five different "close" chips** across modals: ArtifactWorkspace `esc close` / MissionDashboard `esc · close` / SettingsModal `close` / CommandPalette ` kbd:esc` / BranchSessionDialog `cancel`.
3. **View toggles**: ArtifactWorkspace uses icon-only chips (▦ ☰ ◫ ±); SubagentSwarmGrid uses text labels (`grid` / `stack`). Same visual primitive, different vocabulary.
4. **"changes" vs "diff" vs "file changes"** — same concept, 5 different names across ChangesSection / FileChangeCard / ArtifactWorkspace / tool chip badges.
5. **Hover ⋯ menu vs right-click**: Sidebar session rows have both. Conversation messages have only right-click. Swarm rows have neither.
6. **`jump` affordance**: ChangesSection `jump` and FileChangeCard `jump` fire `focusToolCall`. MissionDashboard's `closeAndJumpToTool` does this AND closes the dashboard. Same word, different outcome.
7. **Open externally** has four visual styles: small `open` button; ↗ icon visible-on-hover; `open externally ↗` text button; ↗ glyph on hover. Same action, four idioms.
8. **Tab cluster styling**: MissionDashboard uses `bg-accent/[0.08] ring-accent/[0.22]` for active. ArtifactWorkspace ViewToggle uses `bg-accent/15 text-accent`. SkillCandidatesPanel has its own. **No shared tab primitive.**

##### State-to-state transitions (high frictions)

| From | To | Friction |
|---|---|---|
| Conversation viewing a tool call with file changes | Full-screen diff workspace focused on that change | **high** — must collapse mentally, traverse activity panel, click `jump` (which scrolls back to the chip) or `diff view ↗` (which dumps in workspace with no preselection) |
| Anywhere | Scheduled Jobs Dashboard | **high** — no global path, must ⌘⇧M + click tab |
| Sidebar swarm row | SubagentDetail overlay | **BLOCKED** — sidebar clicks go to `openSessionPane`, not `openSubagent`. SubagentDetail only reachable from Command Palette. |
| Conversation | Computer-use frame timeline / before-after | **BLOCKED** — does not exist |
| Artifact workspace preview | Previous version of same artifact | **BLOCKED** — no version history exposed |
| Streaming session in split view | Cancel current turn | **high** — InputDock unmounts in split view; cancel button is gone; operator must remember ⌘Esc / triple-Esc |
| Conversation | LogStreamModal | **high** — only path is ActivityPanel → diagnostics → expand |
| Kanban card | Reassign / change priority / move column | **high** — no right-click; no inline edit (see K-2) |
| Conversation long output | Top of message / first message | **high** — `scroll to bottom` exists; no `scroll to top` / `jump to message #N` |
| Settings modal | Specific section (e.g. permissions) | **medium** — single scrolling page; `/permissions` doesn't anchor |

##### Slash commands audit

30 commands total · **11 hidden** (`hidden=true`).

`matchSlash` filter excludes `hidden=true` from autocomplete entirely (`slash.ts:44-48`), so operators can't fuzzy-find them. Includes useful commands: `/diagnose`, `/restart-bridge`, `/debug`, `/compaction`.

**Commands that DON'T EXIST** but should (operator looks for them, doesn't find them):

- `/scheduler` — Scheduled Jobs Dashboard has no slash entry
- `/changes` or `/diff` — Changes workspace has no slash
- `/artifacts` — Artifact workspace has no slash
- `/log` or `/logs` — LogStreamModal has no slash
- `/quickswitch` or `/switch` — Ctrl+Tab MRU has no slash entry
- `/widgets` — `show_widget` outputs accumulate in `state.widgets`; no way to browse
- `/inbox` or `/messages` — inter-agent inbox events have no slash filter
- `/up` or `/parent` — ⌘B now maps to "parent" but slash doesn't reflect it
- `/branch` — branching only reachable via right-click on a message
- `/findings` or `/bus` — Findings tab has no direct slash

##### Punch list (interactions) — severity-ordered

| # | Sev | Surface | Finding | Fix |
|---|---|---|---|---|
| I-1 | **S1** | Diff workspace discoverability | Reachable only via 9px chip in ChangesSection, which itself disappears when activity panel is collapsed. **No slash, palette entry, hotkey, title-bar button.** | (a) `/diff` and `/changes` slash; (b) Command Palette entry `Changes Workspace`; (c) global ⌘⇧D hotkey; (d) small `changes` / `diff` chip in title bar that pulses when new change sets land. |
| I-2 | **S1** | Naming consistency for the diff feature | Same surface called `changes` / `diff` / `diff view` / `changes workspace` / `file changes` in different chrome. The user's complaint is partially a *naming* problem. | Pick one canonical noun (recommend `changes` for section + `changes workspace` for modal, with `diff` reserved for per-file payload). Apply consistently across all chrome + slash + palette. |
| I-3 | **S2** | Scheduler dashboard hidden | Only reachable via Mission Dashboard → `scheduler` tab. | `/scheduler` + `/jobs` slash + palette entries; title-bar pill when scheduled jobs about to fire. |
| I-4 | **S2** | Sub-agent detail view orphaned | `SubagentDetail` only opens via Command Palette → search `subagent`. No swarm card, sidebar row, or hotkey routes to it. **Effectively dead UI.** | Either delete the component (if `openSessionPane` is canonical) OR wire it to a clear entry: shift-click on swarm card opens detail; sidebar row shows `detail` affordance. |
| I-5 | **S2** | `/metrics` vs MetricsDashboard collision | `/telemetry` and `/metrics` route to MissionDashboard's `telemetry` tab (which redirects to `activity`). **The actual cross-session MetricsDashboard has no slash command**; only entry is the title-bar `metrics` button (hidden below xl: breakpoint). | Rename slash commands or add `/compaction-metrics` / `/cross-session`. Command Palette entry. Move title-bar button to a visible position. |
| I-6 | **S2** | `/burst` slash help wrong | `slash.ts:34` declares `/burst (Cmd+B)`. ⌘B now maps to `go to parent session`. Slash help is stale. | Update `/burst` description, remove keys hint, or replace with `/parent` / `/up`. |
| I-7 | **S2** | Quick switcher (Ctrl+Tab) undocumented | MRU switcher; not surfaced in InputDock hints, palette, slash, anywhere visible. | Chip in InputDock footer alongside ⌘K / / / @ / ~/. Palette entry. Document in `/help`. |
| I-8 | **S2** | Force-cancel button vanishes in split view | `App.tsx:527: {!splitView && <InputDock />}`. Cancel button lives in InputDock — gone in split view. Operator must remember ⌘Esc / triple-Esc. | Pane-level cancel toolbar above each session pane in split view, OR hoist cancel button to persistent global affordance. |
| I-9 | **S3** | Sidebar open semantics inconsistent with palette | Sidebar: click=replace, ⌘-click=split. Palette: always replace. | Document ⌘-click in palette result rows (modifier badge); let ⌘-Enter on a session result open in split. |
| I-10 | **S3** | Context menus missing on key surfaces | Tool call chips, file-change rows, artifact cards, kanban cards, sub-agent cards have no right-click. Sidebar + message bubbles have rich menus. | Add MessageContextMenu-style menus per surface. At minimum surface existing buttons in a unified menu. |
| I-11 | **S3** | Computer-use diff missing | No before/after, no scrub through frames, no overlay-diff. The most opaque agent activity has the least visual instrumentation. | Frames-list affordance in ComputerLiveView (or new tab in ArtifactWorkspace): scrub through screenshots, optional overlay-diff between adjacent frames. |
| I-12 | **S3** | Artifact versioning / diff missing | An agent that edits the same `.md` three times shows only the latest content. No version history; no diff between versions. | Track artifact revision history (bridge already keeps change sets — wire artifacts to `fileChangeSet` history). Add `history` tab to artifact preview with inline diffs between versions. |
| I-13 | **S3** | Debug drawer hidden | `/debug` is `hidden=true` so never appears in autocomplete. ⌘D works but nothing tells you it exists. | Unhide `/debug` in slash listing (it's not destructive), OR add discreet `debug` affordance in TitleBar right cluster. |
| I-14 | **S3** | Memory section default-collapsed; `/memory` doesn't expand | Sidebar.tsx:130-135 sets `memory: false` default. `/memory` slash sets `focusedPanel=sidebar` and toasts a count but does NOT expand the section. **Operator slash-commands for memory and sees nothing happen.** | Make `/memory` expand the section. Auto-expand on `focusedPanel=sidebar` transitions when memory has new content. |
| I-15 | **S3** | Title-bar action / readout collision | TitleBar mixes interactive buttons (dashboard, metrics, focus, model, workspace toggle, activity toggle) with read-only readouts (ctx, spend, session id) in nearly identical typography. Hover is the only signal. | Clear interactivity affordance on clickable controls (underline on hover, distinct background, icon prefix). Explicit divider before right-hand readout cluster. |
| I-16 | **S3** | Title-bar controls disappear at breakpoints | `workspace` (lg:), `metrics` (xl:), `activity` (xl:), `focus` (2xl:), `slack` pill (2xl:), strategy strip (2xl:). **No overflow `…more` affordance.** | Right-cluster overflow chevron exposing hidden controls, OR refactor to icon-only chips that fit at all sizes. |
| I-17 | **S3** | ⌘F conflict | Conversation + ArtifactWorkspace both register window-level ⌘F handlers. Only call-order saves correctness. | Use capture phase + `stopImmediatePropagation()` once handled. Workspace handler skips conversation toggle when workspace is open. Document precedence. |
| I-18 | **S4** | Five different "close" styles across modals | `esc close` / `esc · close` / `close` / `kbd:esc` / `cancel` — five visual treatments for the same primitive. | Shared `<ModalChrome>` primitive with a single `close` affordance. |
| I-19 | **S4** | Hover ⋯ asymmetry | Sidebar session rows have hover ⋯ + right-click. Conversation messages only right-click. Swarm rows neither. | Either add ⋯ everywhere with right-click pairing, OR remove sidebar's ⋯ in favor of right-click-only. |
| I-20 | **S4** | "Open externally" has 4 visual styles | ↗ as button / hover-revealed icon / text-with-icon / plain icon across surfaces. | Pick one (recommend ↗ glyph + `open externally` label on hover) and use everywhere. |
| I-21 | **S4** | No widget gallery | `show_widget` outputs accumulate in `state.widgets` but no way to revisit past widgets — they're lost in the scroll. | Treat widgets as artifacts; expose a `widgets` tab in ArtifactWorkspace or Mission Dashboard. |
| I-22 | **S4** | Dialog button ordering inconsistent | BranchSessionDialog: cancel left, confirm right. Other dialogs don't follow a consistent order. | Establish "cancel left, confirm right" (or vice versa); apply everywhere. |
| I-23 | polish | InputDock kbd hints blur-fade | Hints (⌘K / / / @ / ~/) only visible when focused. **The hints are a great discoverability hook but vanish exactly when a new operator might be reading the UI to figure out what to do.** | Keep hints visible with subtle opacity when blurred, OR first-run tooltip while input is empty. |
| I-24 | polish | TasksListRailView duplication | Renders for `tab='tasks'` AND for `coordinationStrategy='isolated'` overview. In isolated mode the `tasks` tab is hidden to avoid duplication; in other modes operator can land in `overview` or `tasks` and see different views. Rationale opaque. | Document tab-visibility logic in tooltip / first-time help, OR unify so users understand why `tasks` disappears in some modes. |

##### Surprises (interactions)

- **The diff/changes workspace is unreachable from the global UI.** No slash command, no palette entry, no hotkey, no title-bar button. Only the small `diff view ↗` chip in the activity panel — and only when the panel is uncollapsed.
- **SubagentDetail overlay is orphaned UI.** Only opens by typing `subagent` into the Command Palette. None of the sub-agent cards, sidebar rows, or swarm tiles route to it.
- **`/burst` is stale**: `slash.ts:34` advertises `Cmd+B (demo burst)`, App.tsx ⌘B now means "go to parent session". A comment explains the change; the slash help is wrong.
- **`/telemetry` and `/metrics` route to a different surface than the title-bar `metrics` button.** Real MetricsDashboard has no slash, hidden below xl:.
- **`/debug` is hidden=true.** Operator must already know ⌘D to find the debug drawer.
- **ArtifactWorkspace ⌘F + Conversation ⌘F both register window listeners.** Both fire when workspace is open over conversation; only handler order saves correctness.
- **InputDock disappears entirely in split view** (`App.tsx:527`); visible force-cancel goes with it. Operators must rely on undocumented ⌘Esc / triple-Esc.
- **ScheduledJobsDashboard has NO discoverable entry point** outside Mission Dashboard → scheduler tab.
- **Memory section default-collapsed; `/memory` doesn't expand it.** Operator slashes, sees nothing happen.
- **SwarmMonitor supports j/k vim navigation.** Used nowhere else in the app (not sidebar, not conversation, not kanban, not activity panel).
- **Same noun in 5 different chrome elements** for the diff feature.
- **No before/after view for computer-use screenshots.** The most opaque agent activity has the least visual instrumentation.
- **No artifact version diffing.** Three edits to the same `.md` collapse to one preview.
- **11 of 30 slash commands are `hidden=true`** and blocked from autocomplete. Includes `/diagnose` / `/restart-bridge` / `/debug` / `/compaction`.
- **TitleBar mixes buttons and readouts with identical styling.** Hover is the only differentiation.
- **No `scroll to top` / `jump to message #N` / keyboard message traversal.**

---

#### Cross-cutting themes across both review batches

Five patterns now visible across both batches of reviews. Each is the diagnostic operator-vs-customer question manifesting at a specific layer.

1. **Observation-only surfaces.** Kanban, metrics dashboard, command center (proposed), and to a lesser extent the activity rail all read the system superbly and steer it weakly. The single highest-leverage move across the entire app: **every observability surface needs a "steer" zone** — operator actions inline. K-2, M-6, K-6 are concrete instances.

2. **Hidden / unreachable features.** Six features are functionally hidden (diff/changes workspace, artifact workspace, scheduler, SubagentDetail, QuickSwitcher, DebugDrawer). The user noticed *one* of these (diff viewer). The pattern is broader: **build everything; expose nothing.** The fix is structural — every feature needs (a) a slash command, (b) a Command Palette entry, (c) a hotkey for power users.

3. **Naming inconsistency.** Same concept gets multiple names across chrome (changes / diff / diff view / changes workspace / file changes). The shipped vocabulary needs editorial pruning. **Pick one noun per concept; apply everywhere.** Same theme as the "operator instrument verbs" thread from the vision section.

4. **The instrument is built; its dashboard isn't live.** Two specific tells: MetricsDashboard has no live subscription (manual refresh only); kanban autopilot ignores the countdown data the bridge ships for it. The bridge is honest about what it knows; the renderer is freeze-frame. **Live state without polling clicks** is operator-grade; manual refresh is customer-grade.

5. **Theater surfaces destroy operator trust faster than missing features.** Two examples: the DispatcherBrief looks editable but isn't (kanban K-3); the title-bar wordmark gradient looks like an accent but breaks the palette (onboarding O-5). Either make these levers real or remove them. Operators trust a hand-built tool with gaps; they distrust a polished tool with placeholder buttons.

---

## Part 5 — Unified priority list across all reviews

A cross-review severity ranking. Reading top-down gives the work plan; the ref column points back into each surface's review.

**Codes**: `O-*` Onboarding · `S-*` Active session viewer · `K-*` Kanban · `M-*` Metrics · `I-*` Interactions.

### S1 — Blockers (must-fix-first)

| Ref | Surface | Finding | Vibe impact |
|---|---|---|---|
| **K-1** | Kanban — bucketCards | 4 backend error statuses silently bucket into READY; failure is invisible on the very surface designed to make failure visible. | Hides failure — most customer-shaped move in the system. |
| **K-2** | Kanban — card actions | Operator cannot dispatch, cancel, unblock, or reassign from the board. Observation-only. | The whole "workshop" thesis collapses without operator actions on the board. |
| **K-3** | Kanban — DispatcherBrief | `edit` buttons + radio set have no `onClick` / no state. The brief that's supposed to control autopilot cannot be edited. | Theater is the fastest way to lose operator trust. |
| **I-1** | Interactions — diff workspace | Unreachable from global UI. No slash, palette, hotkey, title-bar entry. Only via 9px chip in collapsible panel. | User explicitly flagged. Symptom of a broader shape (6 features in same condition). |
| **I-2** | Interactions — diff naming | `changes` / `diff` / `diff view` / `changes workspace` / `file changes` — five names for one concept. | Adds to the unreachability. Operators don't know the canonical noun. |

### S2 — High

| Ref | Surface | One-line finding |
|---|---|---|
| **O-1** | Onboarding — HeroWelcome | Generic centered-icon + 2-cards + dashboard CTA shape; circular nudge to an empty dashboard. |
| **O-2** | Onboarding — tip line | Teaches `⌘⇧M` hotkey to an empty dashboard instead of conversation primitives (⌘K / / / @). |
| **O-3** | Onboarding — Settings | No API-key management UI; operators with no env vars hit a toast and a dead end. |
| **S-1** | Session viewer — chrome | No persistent strip above the message stream (session · model · turn-elapsed · live spend · cancel). In focus mode the operator is flying blind. |
| **S-2** | Session viewer — rail order | Governance (drafter + skill candidates) shouts over operational (context + tool timeline + changes). |
| **S-3** | Session viewer — category color rainbow | `#6ba3d6/#5bbb5b/#e0a040/#b080d0/#60c0c0/#d0a040/#b6f2ff` contradicts the explicit palette thesis. |
| **S-4** | Session viewer — long tool runs | 200-call sequential turn = wall of pills. No fold/rollup/time-bucket. |
| **S-5** | Session viewer — cancel receipt | Force-cancel is silent. Operator-feedback contract broken. |
| **K-4** | Kanban — card stale | Bridge emits per-card stale; view only flags agent-stale. |
| **K-5** | Kanban — autopilot countdown | Bridge ships `intervalSeconds` for the countdown; renderer ignores it. |
| **K-6** | Kanban — per-card dispatch | When autopilot off, no way to start a single ready card without flipping the global switch. |
| **K-7** | Kanban — fake progress | Logistic-curve progress percent + 8-minute hardcoded ETA. Both invented. |
| **K-8** | Kanban — flat stat strip | 10 equal-weight tiles; autopilot toggle is one of eleven. |
| **K-9** | Kanban — comments missing | Card comments collected in data, never rendered. |
| **K-10** | Kanban — verify/retry pills | `requiresVerification` and `consecutiveFailures` flow through data, never surfaced. |
| **M-1** | Metrics — header | `compaction metrics` undersells the panel (which now hosts spend, cache, models, profiles, agent decisions). |
| **M-2** | Metrics — Scheduler visual | Inline `<style>` block, different design language. Single biggest off-vibe surface in the app. |
| **M-3** | Metrics — no tools panel | `tool_call_metric` tracked diligently, never aggregated globally. Most operator-obvious miss. |
| **M-4** | Metrics — no live updates | Manual refresh only. Operator watching spend in real time has to keep clicking. |
| **M-5** | Metrics — no comparison | No `vs yesterday` / `vs last week` / baseline anywhere. |
| **M-6** | Metrics — no drill-through | Cannot JUMP into a session from a session row. Drawer is read-only. |
| **M-7** | Metrics — agent internals invisible | Skills / memory / drafter / judge / autopilot economy all written to disk, none reach the dashboard. |
| **I-3** | Interactions — scheduler hidden | Only reachable via Mission Dashboard tab. No slash / palette / hotkey. |
| **I-4** | Interactions — SubagentDetail orphan | Only reachable from Command Palette. None of the sub-agent cards route to it. |
| **I-5** | Interactions — `/metrics` collision | `/metrics` and `/telemetry` route to a DIFFERENT surface than the title-bar `metrics` button. |
| **I-6** | Interactions — `/burst` stale | `slash.ts` declares Cmd+B as burst; binding now means "parent session". |
| **I-7** | Interactions — Ctrl+Tab undocumented | Quick switcher invisible to new users. |
| **I-8** | Interactions — cancel in split view | InputDock unmounts in split view; the visible cancel button goes with it. |

### S3 — Medium (high count; per-row brevity)

Onboarding: **O-4** splash skip hint, **O-5** wordmark gradient, **O-6** splash→hero no breath, **O-7** title-bar reads zero, **O-8** wizard double-dismiss, **O-9** picker "preview" verb, **O-10** bridge offline tooltip-only, **O-11** FALLBACK_MODELS shows fictional models, **O-12** reduced-motion ignores WebGL.

Session viewer: **S-6** context-pressure no tone escalation, **S-7** code blocks no copy/lang/highlight, **S-8** sub-agent spawn silent, **S-9** cancelled tool calls indistinguishable, **S-10** empty pane in split view (404-feel), **S-11** dead `AGENT_TYPE_COLORS` map.

Kanban: **K-11** ActivityFeed passive, **K-12** EmptyColumnSlot generic, **K-13** WipBar no legend, **K-14** JudgeIterationMeter per-segment click, **K-15** AddTaskForm silent failure, **K-16** right rail dead air.

Metrics: **M-8** cooperation tile opaque (no tooltip), **M-9** SpendChart underdense, **M-10** turn histogram bins not clickable, **M-11** no export, **M-12** session-table truncation silent, **M-13** pressure-band caption opaque, **M-14** filter bar busy, **M-15** model-name humanization, **M-16** no keyboard nav, **M-17** Scheduler Metrics tab underbuilt, **M-18** metrics + scheduler split-discoverability.

Interactions: **I-9** sidebar/palette open semantics differ, **I-10** context menus missing on key surfaces, **I-11** computer-use diff missing, **I-12** artifact versioning missing, **I-13** debug drawer hidden, **I-14** memory `/memory` doesn't expand, **I-15** title-bar buttons + readouts indistinguishable, **I-16** title-bar controls vanish at breakpoints, **I-17** ⌘F conflict.

### S4 — Low + polish (compact)

Onboarding: **O-13** workspace-detect stale, **O-14** yolo double-dash, **O-15** strategy-locked invisible state, **O-16** type-size sprawl, **O-17** ICON_SIZE duplicated.

Session viewer: **S-12** PaneChatbox `Reply…` tone, **S-13** InboxChip `[click to expand]` literal, **S-14** rail labels jargon, **S-15** tool-result max-h inconsistency, **S-16** radii proliferation, **S-17** long-run liveness, **S-18** subagent stats roll, **S-19** judge-crash narrator tone weight.

Kanban: **K-17** ActivityFeed source provenance, **K-18** AddTaskForm hotkey, **K-19** DispatcherBrief counterfactuals on read-only brief, **K-20** ConfidenceTrace raw rgb(), **K-21** mass mount animation.

Metrics: **M-19** scheduler cost formatting, **M-20** last-fetched timestamp absolute, **M-21** simultaneous amber tones, **M-22** SpendChart axes locale.

Interactions: **I-18** five close-chip styles, **I-19** hover ⋯ asymmetry, **I-20** four open-externally idioms, **I-21** widget gallery missing, **I-22** dialog button order inconsistent, **I-23** InputDock hints blur-fade, **I-24** TasksListRailView duplication.

### Recommended order of attack

Tackling them by item-count is wrong — many polish items compound; some blockers are easy. The right order pairs **highest-impact** with **cheapest-to-build**:

**Week 1 — Trust restorers** (each unblocks something specific):
1. **K-1, K-2, K-3** — kanban actions + error visibility + DispatcherBrief honesty. Three together restore operator trust in the kanban surface.
2. **I-1 + I-2** — diff workspace discoverability + naming. The user's stated complaint. Add slash + palette + hotkey + canonical noun.
3. **S-5** — cancel turn receipt. Smallest change with the largest "instrument feels alive" payoff.

**Week 2 — Reachability** (the hidden-features pattern):
4. **I-3, I-4, I-5, I-7** — scheduler, SubagentDetail, metrics-slash collision, Ctrl+Tab discoverability. The pattern that I-1 is one instance of.
5. **I-13, I-14** — Debug drawer + memory expansion. Sibling reachability fixes.
6. **O-3** — API-key management. Closes the worst first-run dead-end.

**Week 3 — Operator chrome + steering**:
7. **S-1** — top-of-conversation strip. Anchors every other operator signal.
8. **S-2** — activity rail reorder.
9. **K-5, K-6, K-9** — autopilot countdown + per-card dispatch + comments. Steering controls.
10. **M-2, M-4, M-6** — Scheduler visual reskin + live updates + drill-through. Metrics becomes a real instrument.

**Week 4 — System discipline + polish**:
11. **S-3** — replace tool category color rainbow.
12. **O-1** — replace HeroWelcome with an instrumented panel (the morning room or the panel that prefigures it).
13. **Token consolidation pass** (cross-cutting from Part 1's aesthetic system inventory): collapse 1,137 arbitrary text sizes to a 7-step scale; route `rgba(168,212,252,*)` through `accent`; replace bespoke off-palette bg hexes; document radii hierarchy. Invisible polish; compounds.
14. **Long tail of S3/S4/polish items**, in any order. Most can be batched into a single "naming + tab + close-chip" cleanup PR.

Reading top-down: **after Week 2 the user's stated complaints are gone**; after Week 4 the diagnostic across every surface should resolve to `operator`.

---

### 2026-06-03 — Scheduled jobs + past runs (detour review)

Standalone deep-read of the surface that visualizes schedules, lists past runs, and shows run details. Done as a focused single-surface review (not parallel) because the surface is one component plus its store, and the user called it out specifically as "lousy" — the headline is "what would a redo look like."

**Files inspected**:
- `src/renderer/components/ScheduledJobsDashboard.tsx` (676 lines — the entire visible surface)
- `src/renderer/state/scheduler-store.ts` (284 lines — data model + bridge IPC wrapper)
- `src/renderer/components/MissionDashboard.tsx` (mount path: tab 'scheduler')
- `~/.freyja/schedules/{jobs,runs,events,memory,metrics,outputs}/` (on-disk artifact shape)
- `bridge/scheduler/{daemon,service,runtime,scheduling,persistence,models,memory}.py` (data shapes available)

**Headline finding** (one sentence): *Freyja already collects every signal an operator could want about a scheduled run — prompt, exact output, error trace, token usage, cost, delivery reports per sink, attachments, iteration count, execution session id, permission snapshot — and exposes none of it past a 14-character truncated `run_id` in a table with no row click handler.*

---

#### Pass 1 — Vibe

Adjectives: **admin-panel · 2010-html · ported-in · parallel-design-system · semantic-but-flat · color-literal-soup · sans-serif-amid-mono · refresh-button-vibe.**

- **Who made this?** It reads like a port from another codebase. Components use `<h2>`/`<h3>`/`<h4>`/`<dl>`/`<dt>`/`<dd>`/`<table>` semantically — admirable HTML hygiene — but every styling decision contradicts the rest of Freyja. Inline `<style>` block at the bottom of the file (88 lines, line 588) with `.sjd-*` class prefix; sans-serif fallback (`-apple-system, BlinkMacSystemFont, sans-serif`) for the wrapper; rgba color literals (`rgba(80,140,220,0.3)` for the submit button); 2/3/4/6px radii. None of it matches `glass`, hairlines, Geist Mono, Fraunces, the steel-blue accent ramp, or the muted-status palette.
- **Era**: 2010 admin-panel HTML. Pre-Linear, pre-Granola, pre-Raycast. The semantic structure is older-Internet-good; the styling is older-Internet-bad.
- **Human presence**: low. One genuinely warm bit in the empty state: *"Create one from the New tab, ask Freyja in chat ('remind me in 30 min to check the deploy'), or use /freyja remind … on Slack."* That's it.
- **Screenshot for inspiration**: no.

**Pass-1 verdict**: *The single most off-vibe surface in the app. Everywhere else Freyja drifts into customer territory at the edges — this surface IS the customer territory. A reskin to match the rest of the chrome moves the diagnostic by itself; the deeper functional gaps need a redo.*

#### Pass 2 — Eye-path

**First focal point**: the tab strip — `Jobs (5) / Runs / New / Daemon / Metrics / ↻`.
**Intended focal point**: the active job + its next-fire information.
**Hierarchy shape**: **flat**. 12px font everywhere. Job names use `font-weight: 600` at the same size as the metadata below them. Status pills are the only color signal — but in a non-Freyja palette.

Competing focal points:
- The tab strip (sans-serif `<h2>` "Scheduled Jobs", 16px) reads as the page title.
- The Jobs list rows (12px name + 11px metadata + status pill).
- The detail aside (380px right column with a `<dl>` of properties).
- The refresh `↻` chip floating at the right of the tab strip.

**Primary action findable in <2s**: ambiguous. *Run now* exists but only inside the right-hand detail aside, only when a job is selected. *Create* lives in a tab that takes a click to discover. *Pause/Resume* same. The most-common operator move ("see what happened on the run that just fired") has *zero* visible action — the runs table has no click handler.

#### Pass 3 — Functional walkthrough

| Context | Designed | Note |
|---|---|---|
| Jobs list empty (first run) | ✓ | Honest text: *"Create one from the New tab, ask Freyja in chat, or use /freyja remind on Slack."* The only humane microcopy in the file. |
| Runs view empty | ✗ | `"No scheduled runs yet."` — flat label, no signpost to create one. |
| Job detail "no runs yet" | ✓ (minimal) | One-line italic "No runs yet." |
| Per-run detail | **✗ (DOES NOT EXIST)** | **The single biggest gap.** Each run row carries: `run_id` (truncated to 14 chars + `…`), status pill, started timestamp, duration, sinks. No click handler. **`output_text`, `error`, `delivery_reports[].error`, `output_attachments`, `input_tokens`, `output_tokens`, `cost_usd`, `iterations`, `execution_session_id` — all stored, all invisible.** The `output_text` Preview column shows the first 80 characters and that's it. |
| Loading | ✗ (text only) | `"Loading metrics…"` / `"Loading daemon status…"` — no spinner, no shimmer, no progress. Mid-fetch the panel just sits blank. |
| Error states | ✗ | The create form catches errors and shows `"Error: {message}"` inline (line 353). No toast. No retry button. No re-validate path. Other tabs swallow errors entirely (`.catch(() => {})` on every IPC). |
| Live updates | ✗ | 30-second `setInterval` (line 47) firing **four** IPC calls in parallel (`listJobs + recentRuns + metrics + daemonStatus`). No live event subscription. No "data is N seconds stale" indicator. No way to tell a fetched-then-mutated state has gone live. |
| Schedule visualization | ✗ | A single `formatSchedule()` line per job. `"cron(0 9 * * *) [UTC]"` or `"every 1h"` or `"once at 2026-06-01T09:41:05.058943+00:00"`. **No calendar, no timeline, no next-N-fires preview, no fire history overlay.** |
| Create flow validation | ✗ | The `when` field accepts a natural-language phrase; the bridge parses it; failure is reported as a returned error string after submit. No live preview of "this would fire next at: …", no chip-based stepper, no example presets. |
| Run "rerun with edit" | ✗ (does not exist) | Can re-fire a job (`Run now`), but cannot run-with-a-tweaked-prompt. |
| Filter runs | ✗ (does not exist) | RunsView dumps the last 100 sorted by start time. No filter by job, status, date range, sink, cost, duration. No search. |
| Sort runs | ✗ | No column sort. Order is bridge-supplied. |
| Failed-run notification | ✗ | Failed runs show `dcb46a` (muted amber) in a table — easy to miss. No toast. No global badge. No sound. **The operator finds out their cron failed by happening to be on the Runs tab when it lands.** |
| Daemon status badge | ✗ | The "is the daemon running" signal lives inside the Daemon tab. **Operator has no global indicator that scheduled jobs are firing.** If the daemon is uninstalled, the operator's UI looks identical to "daemon installed + healthy" — until 24 hours later when they notice nothing fired. |
| Sink delivery detail | ✗ | Chips show `slack` / `desktop` / `filesystem` / `webhook` in green or red. Tooltip carries `error` text. No view of "what message landed in Slack", "which file was written", "what response did the webhook return". The `delivery_reports[].artifact_ref` field is shown only as a tooltip. |
| Jump from run → session | ✗ | `execution_session_id` exists in the run record (e.g. `scheduler:sched_260462a29611.ephemeral`). **Never linked.** Same gap as M-6 (metrics drill-through). |
| Job duplication | ✗ | No "duplicate this job" button. To make a sibling cron with one parameter changed, the operator must hand-type the whole thing again. |
| Test prompt before saving | ✗ | The Create form has no "dry-run this prompt now" affordance. |
| Permission snapshot visibility | ✗ | Every job carries a `permission_snapshot` (`yolo` / `requested` / etc.) — **never shown in the detail aside.** A scheduled job running with `yolo` permissions is the single highest-risk thing the operator can ship, and the UI hides which tier it's running at. |
| Cost trend per job | ✗ | Per-run `cost_usd` shown in the row, per-job `fire_count` shown in the row, 24h total in metrics. **No per-job cost trend over time.** |
| Iteration count | ✗ | The `iteration` field exists (runs can iterate; the persistent_job_session execution mode loops). Never surfaced. |
| Skills / model / coordination strategy | ✗ | Job records carry `skills_to_load[]`, `model_id`, `coordination_strategy`. The detail aside shows none of them. |

**Microcopy smells**:

- Tabs are mixed-case with parens: `Jobs (5) / Runs / New / Daemon / Metrics`. Should be lowercase tracked-spaced labels matching the rest of the app: `jobs · runs · new · daemon · metrics`.
- `"× Close"` button label in detail aside reads as a typo (literal `×`).
- Sparkline label: `"Runs by hour of day (last N)"` — hour-of-day bucketing, not date bucketing. For a daily-cron operator, this is the wrong axis: they want to know "did it fire every day this week?" not "what hour does it usually fire?"
- Job row metadata: `next: 6/2/2025, 11:00:00 AM (4h)` — absolute timestamp + relative. The absolute uses `toLocaleString()` which is locale-dependent and clashes with the mono everywhere else.

**Stress findings**:

- 100+ jobs: `JobRow` renders flat with no virtualization. List grows downward indefinitely; scroll-jank likely on long lists.
- 100+ runs in RunsView: `slice(0, 100)` in the store caps it. A heavy-scheduler-user with 200+ runs/day sees only the last hour.
- Long `output_text`: the table preview clips at 80 chars. The full output is never accessible.
- Multi-iteration runs (`iteration > 0`): the run table has one row per iteration with the same `run_id` prefix. Hard to tell which iteration produced which row.
- Job with no sinks: shows `sinks: —` (good). But the runs from that job show no delivery_reports — the operator has no way to know the run produced output that went nowhere.

#### Pass 4 — Aesthetic discipline

| Token | Found | Note |
|---|---|---|
| **Color** | `#6acc88` (ok), `#d4b542` (paused), `#6aa2dd` (running/active), `#dc6a6a` (failed), `#dcb46a` (partial_failure), `#dcb46a` (warn), `rgba(80,140,220,0.3)` (submit), `rgba(140,180,220,0.5)` (sparkline) | **None match the Freyja palette.** App uses `ok #a8b0a8` (muted sage), `warn #b8a078` (muted ochre), `danger #b48282` (muted rose), `accent #a8d4fc` (steel blue). The dashboard ships its own brighter, more saturated palette — strictly off-vibe. |
| **Typography** | `<h2>` 16px / `<h3>` 14px / `<h4>` 13px / body 12px / 11px / 10px | The wrapper's `font-family` falls back to `var(--font-sans, -apple-system, …)` — sans-serif, not mono. **The only sans-serif region in the whole app outside of Fraunces serif h1s.** Headings use semantic tags with default browser weights. |
| **Borders / radii** | 2px / 3px / 4px / 6px | App standard is `2/4/6/12/18` per the radii cleanup proposal. 3px is unique to this file. |
| **Shadows** | None | Other surfaces use the glass ladder (`.glass-raised`, `.glass-strong`); this dashboard uses opaque `rgba(255,255,255,0.04)` panels with no shadow. |
| **Spacing** | `padding: 12px 16px / 8px / 16px / 24px / 4px 10px / 6px 14px` | Arbitrary px, not aligned to Tailwind's 4px scale. |
| **Token discipline** | **bespoke** | The dashboard does not use a single Tailwind class. The entire file styles via the inline `<style>` block at line 588. |
| **Grayscale test** | barely passes | Status colors carry hierarchy via hue, not weight; in grayscale the four statuses collapse to nearly-equal greys. |

**Verdict**: this is the only surface in Freyja that is not part of the system. Reskinning to use Tailwind + the existing palette + Geist Mono + glass surfaces would move the file from ~80% off-vibe to ~5% off-vibe with no functional change.

#### Pass 5 — Density and breath

| Region | Density | Earned | Note |
|---|---|---|---|
| Tab strip + h2 | balanced | ✓ | The chrome itself is fine — 12-char tabs + 16px page title. |
| Jobs list | **cramped + flat** | ✗ | Every row is a 64-72px tall card with name + id + status pill + cadence + next-fire + sinks + fires count all jammed together at near-identical typography. The eye has no resting point inside a row. |
| Job detail aside | **cramped** | ✗ | 380px wide column with a 12-row `<dl>` followed by a `<details>` for the prompt followed by a runs table. Three modes of content, no visual breaks, no breathing room. |
| RunsView (global runs table) | **cramped** | ✗ | Dense table, no zebra striping, no row separators (only a 1px border-bottom rgba(255,255,255,0.05)), no expand-on-click. 100 rows of identical mono text. |
| CreateView | **evasive (sparse)** | ✗ | Single 720px-wide column of inputs. No examples, no presets, no live preview. The `when` field's placeholder ("e.g. \"every weekday at 9am\"") is the only guidance; the operator types and finds out at submit time whether it parses. |
| DaemonView | balanced | ✓ | Five-row `<dl>` + install/uninstall button. About the right amount for a system-y page. |
| MetricsView | balanced | ✓ | 9 KPIs + sparkline + top-jobs table. Standard admin shape. |

**Overall**: the surface oscillates between cramped (lists, tables, detail) and evasive (create form). Density never matches the data shape — a sparse form for a complex domain, a cramped table for content that should expand.

#### Diagnostic — operator or customer?

**Score: customer.**

This is the only surface in the codebase scoring full customer. Reasons:

1. **Parallel design system**: inline CSS, mismatched palette, sans-serif headers, 2010-admin-panel HTML — every visual choice opts out of Freyja's vocabulary.
2. **Hidden depth**: the data the bridge collects is genuinely operator-grade (prompt, output, error, attachments, tokens, cost, sinks, session id), but the UI exposes it at the depth of "the run finished, here's a 14-char id, click somewhere else to see anything more" — and there's no somewhere else.
3. **Discoverability is one path**: Mission Dashboard → scheduler tab. No slash command, no palette entry, no global badge, no daemon-status indicator in the title bar. The operator does not know whether their cron is running until 24h later when they look.
4. **Theater absent but emptiness present**: at least there's no fake-progress to lie about. The honesty here is the surface admitting it has nothing — but that's customer-grade by inaction, not operator-grade by design.

A reskin + a run-detail drawer + a daemon-status pill in the title bar would move this from `customer` to `mixed_lean_operator` in a single PR. The full redo described below moves it to `operator`.

#### Utility findings — operator tasks the surface fails

The user's explicit framing: visualize schedules, see past runs, drill into a run. Here's what an operator cannot accomplish today:

1. **See the full output of a successful run.** The Preview column shows 80 chars. The whole `output_text` is unreachable. For a scheduled HN-digest job, the entire deliverable is hidden behind a truncated preview.
2. **See why a failed run failed.** The `error` field on the run record is never rendered. The `delivery_reports[].error` only appears in a hover tooltip.
3. **See the prompt as fired.** Job-level prompt is in a collapsed `<details>`; run-level prompt (which can be edited per-fire when the job is iterating) is invisible.
4. **Compare two runs of the same job.** No diff, no side-by-side, no history. For a daily research run, the operator cannot answer "did today's output match yesterday's structure?"
5. **See what was delivered to Slack / a webhook / disk.** The chips say `slack ✓` or `webhook ✗`. The actual delivered payload is invisible. The `artifact_ref` (the desktop session id, the file path, the webhook URL response) lives only in a tooltip.
6. **Jump from a run into its session.** `execution_session_id` exists. The UI never links to it. Same complaint as M-6 for the metrics dashboard.
7. **Filter runs by anything.** No filter by job, status, date range, sink, cost, duration.
8. **Search runs by output content.** No search.
9. **Re-run with a tweaked prompt.** Run-now refires verbatim. To tweak, the operator edits the job — losing the original.
10. **See the next 5 fires for a cron.** Only `next_fire_at` is shown. For a complex cron or a self-paced schedule, the operator cannot preview the upcoming cadence.
11. **See firing history as a calendar.** No "did it fire every day this week?" view. The sparkline buckets by **hour of day**, which answers a question almost no operator asks.
12. **See cost trend per job.** Per-run cost shown; per-job 24h total shown. No 7-day trend, no 30-day trend, no "this job got 5x more expensive in the last week" anomaly.
13. **Tell the daemon is running.** Daemon status only inside the Daemon tab. No global badge.
14. **Get notified when a scheduled run fails while you're elsewhere.** No toast, no sidebar pip, no Slack notification.
15. **See the permission tier the job runs at (`yolo` / `requested` / etc.).** Stored, hidden — the security-critical field invisible.
16. **See the model + skills + coordination strategy a job uses.** All stored, all hidden.
17. **Pause + resume the whole scheduler from outside this tab.** No global toggle.
18. **Duplicate a job to create a sibling.** No "duplicate" button.
19. **Test a prompt before saving as a scheduled job.** Create form is all-or-nothing.
20. **See iteration history for a multi-iteration run** (e.g. a persistent-job-session that ran 3 turns at the latest fire). The `iteration` field is never used.

#### Aesthetic system — leakage inventory

For the planning-doc record (to cross-reference Part 1's system audit):

- **Hex colors not in the palette**: `#6acc88`, `#d4b542`, `#6aa2dd`, `#dc6a6a`, `#dcb46a` — five bespoke status hues, none from `ok/warn/danger/accent`.
- **Rgba literals not in the palette**: `rgba(80,140,220,*)`, `rgba(140,180,220,0.5)`, `rgba(255,255,255,0.03..0.2)` — twelve+ unique values.
- **Sans-serif region**: only place in the app outside Fraunces h1s.
- **Radii**: 2, 3, 4, 6px — three of the four don't match the system.
- **Spacing**: 4 / 6 / 8 / 10 / 12 / 14 / 16 / 24px — partially aligned to 4px scale but the 6 / 10 / 14 values land between Tailwind tokens.
- **No glass surfaces**. No hairlines. No `tabular-nums`. No `font-feature-settings`.

#### References

| Product | Direction | Note |
|---|---|---|
| Sidekiq / Resque admin panels | **negative** | This is what the dashboard most resembles — a generic Ruby admin panel. Functional but anonymous. |
| Cron (the Mac app) | positive | Calendar view of scheduled events with click-to-detail, color-coded status, gentle motion. The kind of "schedule visualization" Freyja's surface aspires to. |
| GitHub Actions runs page | positive | Per-run click-into-detail with full logs, status per step, retry, jump to source. The minimum-viable "see what happened on this run." |
| Vercel deployments dashboard | positive | Time-grouped list of runs with inline preview, click-to-full-detail, easy re-run, environment indicator. |
| Linear cycle view | positive | A timeline showing scheduled work over upcoming and recent days — operator vocabulary for "what's about to fire." |
| Datadog Synthetic Tests | positive | Per-test run history with screenshots, response bodies, error traces, comparison to prior runs. The drill-in pattern. |
| Anything labeled "Cron Jobs" with a list of cron expressions | negative | The space we don't want to be in. |

#### Punch list (severity-ordered)

| # | Sev | Surface | Finding | Fix |
|---|---|---|---|---|
| **SCH-1** | **S1** | Run rows have no detail view | Cannot click a run to see `output_text`, `error`, `delivery_reports[].error/artifact_ref`, `output_attachments`, `input_tokens`, `output_tokens`, `cost_usd`, `iterations`, `execution_session_id`, the prompt-as-fired. All stored, all unreachable. | Each run row becomes a button. Clicking opens a drawer (same vocabulary as the kanban DetailDrawer) with: status header · prompt · output · attachments · per-sink delivery section · tokens + cost · iteration breakdown · `Open session` button (`openSessionPane(execution_session_id)`). |
| **SCH-2** | **S1** | Parallel design system | Inline `<style>` block, `sjd-*` classes, sans-serif headers, bespoke palette, 3px radii. The single most off-vibe surface in the app. | Reskin to Tailwind + glass surfaces + Geist Mono + Fraunces h1 for the page title + the canonical `ok/warn/danger/accent` palette. Delete the inline `<style>` block. Use `DetailDrawer`/`StatusPip`/the kanban-style empty-state vocabulary. |
| **SCH-3** | **S1** | Schedule "visualization" is one line of text | `cron(0 9 * * *) [UTC]` tells you nothing about when it actually fires. | Visualize schedules: (a) a "next 5 fires" preview list with relative times; (b) a 14-day timeline overlay showing past fires (✓ ok / ✗ fail) and upcoming fires; (c) for cron, a small expanded summary ("daily at 09:00 UTC · next: tomorrow 09:00 (12h)"). |
| **SCH-4** | **S2** | Failures are silent | No toast when a scheduled run fails. Failed-status color (`#dcb46a`) is close to warn-amber and easy to miss. No global badge. | Bridge fires a system_event for failed runs; render a toast + sidebar pip. Add a title-bar "scheduler" chip that goes danger-tinted on failure within the last hour. |
| **SCH-5** | **S2** | No live updates | 30s `setInterval` polling four IPCs in parallel. No event subscription, no "data is N seconds stale" indicator, no auto-refresh after a `run_job_now` until the next poll lands. | Subscribe to `scheduler_response` event firehose; treat `metrics` / `recent_runs` as push-updated. Keep 30s polling as a fallback. Surface a "live" indicator + a manual refresh that animates. |
| **SCH-6** | **S2** | No jump-from-run-to-session | `execution_session_id` lives in the run record; never linkified. Same gap as M-6 for the metrics dashboard. | `Open session` button in the run detail drawer; clickable session id throughout. |
| **SCH-7** | **S2** | Daemon status invisible outside the Daemon tab | If the daemon is uninstalled, the rest of the UI looks identical to "daemon installed and healthy." | Title-bar pill: `daemon · running (pid 12345)` / `daemon · not installed` / `daemon · crashed N min ago`. Click → opens the Daemon tab. |
| **SCH-8** | **S2** | Create form lacks preview / chip-based when picker / validation feedback | Operator types a natural-language phrase and finds out at submit time whether it parses. | Live preview of "next 5 fires" as the operator types. Chip-based presets: `daily at 9am · every weekday at 9am · every Monday · once tomorrow · in 30m · every 4h`. Submit-time validation becomes confirm-time confirmation. |
| **SCH-9** | **S2** | Filter / search on runs | No filter by job, status, date range, sink, cost, duration; no search by output content. | Filter bar above RunsView: job dropdown · status chips · date range · sink chips · cost > $X. Search inputs `output_text` via bridge IPC (add `scheduler.search_runs`). |
| **SCH-10** | **S2** | Permission tier hidden | Every job carries `permission_snapshot` (`yolo` / `requested` / etc.). The single most security-critical field never rendered. | Job row + detail header: a permission chip in the same vocabulary as the SettingsModal tier badges (danger-tinted for `yolo`, warn for `requested`, neutral for `block_dangerous`). |
| **SCH-11** | **S3** | Tab labels mixed-case with parens | `Jobs (5) / Runs / New / Daemon / Metrics`. | Lowercase tracked-spaced: `jobs · runs · new · daemon · metrics` with a small count next to the active label. |
| **SCH-12** | **S3** | Jobs list is flat | 64-72px tall rows, every metadata field at the same weight. | Two-row layout per job: top row = name + status pill + next-fire countdown (mono `next: 4h 12m`); bottom row = compact cadence + sinks chips + fire count + per-job spend sparkline. Group rows by status. |
| **SCH-13** | **S3** | RunsView dense table without affordances | 100 rows of identical mono text, no expand-on-click, no zebra. | Replace with a card-per-run list: status pip · job name · started (relative) · duration · cost · sink chips. Click expands inline to show output preview + error if any. |
| **SCH-14** | **S3** | No cost trend per job | Per-run cost shown; no 7-day trend, no 30-day trend, no anomaly callout. | Per-job sparkline showing fire count + cost over 7 days. Anomaly callout when this week's cost > 2× the trailing 4-week average. |
| **SCH-15** | **S3** | Sinks shown by kind only | `slack` / `desktop` / `filesystem` / `webhook` — operator doesn't see the destination. | Each sink chip carries its destination inline: `slack #ops`, `desktop session-abc`, `file ~/Documents/digest.md`, `webhook example.com/x`. |
| **SCH-16** | **S3** | run_id truncated to 14 chars in the table | Just hard to refer to in conversation. | Show full id on hover; copy-on-click. |
| **SCH-17** | **S3** | No `/schedule` slash command, no palette entry, no hotkey | Discoverability: one path through Mission Dashboard. | Add `/schedule` (and aliases `/jobs`, `/cron`) to `slash.ts`; add Command Palette entry `Scheduled Jobs`; global hotkey `⌘⇧S` opens the dashboard. |
| **SCH-18** | **S4** | Duplicate-a-job button missing | To create a sibling cron with one parameter changed, hand-retype the whole thing. | "Duplicate" action in the job detail aside; opens the Create form pre-populated. |
| **SCH-19** | **S4** | Test prompt before saving | Create form is all-or-nothing. | "Test now" button on the Create form — fires the prompt into a one-shot session, shows the output inline, no schedule created. |
| **SCH-20** | **S4** | Iteration breakdown invisible | Multi-iteration runs collapse into rows that share the `run_id` prefix; hard to tell which iteration produced which row. | Group iterations under the run id; show a "3 iterations" chip with expand. |
| **SCH-21** | **S4** | Model / skills / coordination strategy hidden | Stored, never shown. | Detail aside gains chips: `model · gpt-5.5`, `skills · 3`, `strategy · bus`. Clickable to filter the runs by model. |
| **SCH-22** | **polish** | Sparkline buckets by hour-of-day | Answers "what time of day do jobs run" — almost nobody asks this. The relevant axis is days/weeks. | Sparkline buckets by date; toggle between "last 24h by hour" and "last 14 days by day". |
| **SCH-23** | **polish** | Durations rendered as `117.9s` | Should be `1m 58s`. | Reuse `humanizeSeconds` everywhere or `formatDuration` from `lib/format`. |
| **SCH-24** | **polish** | Timestamps use `toLocaleString()` | Locale-dependent. | Match the rest of the app — relative times + mono digits. |
| **SCH-25** | **polish** | No keyboard navigation | Tables are mouse-only. | j/k row navigation, Enter to open detail, Esc to close. Match the SwarmMonitor convention noted in I-21. |

#### Surprises

- **Delight**: every signal the operator could want about a run is already collected. `output_text`, `output_attachments`, `input_tokens`, `output_tokens`, `cost_usd`, `iterations`, `delivery_reports[]`, `execution_session_id`. The data is there. The UI just doesn't surface it.
- **Delight**: the Create form's `when` accepts natural language (`"every weekday at 9am"`, `"in 30 minutes"`, `"tomorrow at 5pm"`). That's an operator-grade parser hiding behind a 2010-style form.
- **Delight**: the empty state's microcopy. *"Create one from the New tab, ask Freyja in chat ('remind me in 30 min to check the deploy'), or use /freyja remind on Slack."* This is the only sentence in the file that sounds like Freyja.
- **Friction**: the inline `<style>` block at the bottom of the file (line 588) is so isolated from the rest of the app's CSS that nothing about Freyja's tokens, glass surfaces, or mono identity could possibly be inherited. The opt-out is structural.
- **Friction**: `30_000` (line 47) polling interval is the only data freshness mechanism. The bridge publishes `scheduler_*` events on every state transition; the dashboard subscribes to none of them.
- **Friction**: the `CreateView` form's `Sinks` field is a comma-separated string parsed at submit time. No chip-based picker, no validation per chip, no destination autocomplete. Operator types `slack,filesystem` and finds out post-submit whether either parsed.
- **Friction**: 9 lines of duplicate IPC plumbing in the store for `pause_job` / `resume_job` / `remove_job` — the switch in `handleEvent` (lines 198-204) deliberately does nothing on these subtypes, relying on the caller to manually call `listJobs()` after each mutation. Predictably, the caller (`JobDetail.act`) does call it. But the pattern is silently lossy: the next paint may show stale data if the operator switches tabs faster than the round-trip.

---

#### The redo, sketched

The user's explicit framing: "redo how we visualize schedules, past runs, see more details on the run that happened for a schedule." Below is the architectural sketch — what would replace the current ScheduledJobsDashboard if we rebuilt rather than patched. (Detailed visual design lands when we start the build; this is the surface map.)

**Top-level layout**: a single dashboard at `/schedules` (and `⌘⇧S` to open). Four primary regions, no tabs:

1. **Header strip** (60px) — `objective`-style page title in Fraunces editorial serif: *"Scheduled jobs · 5 active · next fire in 4h 12m"*. Right-side: `daemon · running (pid 12345)` pill, `+ new schedule` button, `↻ refresh / live` indicator. Reuses the kanban session-header vocabulary.

2. **Schedules section** (left column, 60% width) — a vertical list of *schedule cards*, not table rows. Each card is glass-raised, hairline-bordered, with:
   - **Title row**: name + status pip (`active · paused · disabled · failed-last-fire`) + permission chip + cadence ("daily · 09:00 UTC")
   - **Timeline strip**: a 14-day horizontal bar showing past fires (✓ green dot / ✗ red dot / · grey for no-fire) and upcoming fires (dim outline dots) over the next 7 days. Click a dot to jump to that run's detail drawer.
   - **Metrics row**: per-job 14-day cost sparkline · last-fire duration · success rate · sinks chips with destination inline
   - **Actions row**: revealed on hover — `Run now · Pause/Resume · Edit · Duplicate · Delete`. Action confirmation matches the kanban operator-action pattern.

3. **Runs section** (right column, 40% width, sticky) — a scrolling feed of recent runs, newest first. Each entry:
   - **Compact header**: status pip · job name · started Xm ago · duration · cost · sink chips
   - **Click expands inline to a 200px-tall detail block**: output preview (full output_text, scrollable), error if any, iteration breakdown, delivery reports per sink with destination + payload reference, prompt-as-fired (collapsible), `Open session` button linking to `execution_session_id`.
   - **Filter chips above the feed**: job, status, date range, sink. Search input filters by `output_text` (new bridge IPC `scheduler.search_runs`).
   - **Live indicator** when a run is mid-flight: a streaming progress pip with elapsed time, links to the executing session.

4. **New-schedule modal** (replaces the `New` tab):
   - Step 1: prompt + optional name. Big textarea. *"Test now"* button executes the prompt in a one-shot session and shows the output inline before committing.
   - Step 2: when. Chip-based presets (`daily 9am · weekdays · in 30m · once at...`) + free-form natural-language field with live "next 5 fires" preview.
   - Step 3: sinks. Chip-picker with destination autocomplete (Slack channels from the workspace, session ids, filesystem path picker, webhook URL).
   - Step 4: execution mode. Three cards (`new session per fire / persistent session across fires / fire into the current session`) with explainer subtitles.

**Run detail drawer** (referenced from anywhere): full-height right-side drawer (reuses kanban `DetailDrawer`). Sections:
- Header: status pip + job name + duration + cost + permission chip
- Output: full `output_text`, monospace, with copy + open-in-workspace
- Attachments: `output_attachments` as artifact cards
- Delivery: per-sink success/failure, destination, payload preview, response if any
- Metrics: tokens (in/out/cache), iteration count, execution session link
- Prompt: collapsible — the prompt-as-fired, copyable
- Footer: `Re-run · Re-run with edits · Open session · Duplicate job`

**Daemon strip** (always visible in the page header): pid + running state + log path + install/uninstall.

**Metrics** (no longer a separate tab; integrated into the header strip + per-schedule cards): cost trend per schedule lives in the schedule card; global cost lives in the header; anomaly callouts surface as warn-tinted chips in the affected schedule card.

The core moves are:

- **Schedule cards replace the jobs table** — each schedule gets its own timeline, sparkline, and inline actions.
- **Click-to-detail everywhere** — every run is a button; the drawer renders the data that's been hiding.
- **Daemon + global state in the header** — operator knows whether the scheduler is running without going hunting.
- **Live by default** — bridge events drive updates, polling is fallback.
- **`/schedule`, `⌘⇧S`, Command Palette entry** — discoverable from anywhere.
- **Tab strip → single page** — five tabs collapse into one because the underlying domain is one ("schedules and their runs"); separating Jobs / Runs / Metrics was a 2010 IA reflex.

Reskinning + this rebuild together turn `customer` → `operator`. The reskin alone (SCH-2) moves the diagnostic significantly even without the full redo; SCH-1 (run detail drawer) is the highest single-functional-impact move.

---

#### Shipped 2026-06-06 — full redo (Option B)

The user picked Option B ("ultrathink") — full redo, not patches. What landed in this pass:

**Bridge**
- `scheduler.preview_next_fires(jobId | when | schedule, n=5)` IPC (`bridge/freyja_bridge.py`). Iterates `compute_next_fire` with `last_fire` chaining so it works for `once · interval · cron · self_paced`. Powers the live "next 5 fires" preview in the new-schedule modal and the timeline strip's future segment.
- Push events (`scheduler_run_*` / `scheduler_job_*`) now flow into `scheduler-store` directly — the renderer no longer polls every 30s. App.tsx routes any `scheduler_*` event into the store; the store reacts to lifecycle events to update jobs, runs, and recent feed.

**Reach paths** (SCH-17 closed)
- `⌘⇧S` global hotkey opens the modal (App.tsx).
- `/schedule`, `/schedules`, `/jobs`, `/cron` slash commands (slash.ts → store.ts).
- Command Palette entry "Scheduled Jobs" with live job count (CommandPalette.tsx).
- TitleBar `SchedulerPill` — visible only when ≥1 job exists; shows active count + next-fire countdown + daemon-state dot. Click → modal.

**The modal itself** — `ScheduledJobsDashboard.tsx` rewritten end-to-end (was 676 lines of tabs + table + inline `<style>`; now ~900 lines of glass + Fraunces + Tailwind utilities).
- Header strip: Fraunces h1 (live count), `next: 4h 12m · "Daily digest"` chip, daemon chip (collapsible strip), `+ new schedule`, `esc · close`.
- Schedule cards (left) replace the jobs table — each card has: status pip, Fraunces serif name, cadence + next/last/fire-count meta row, **14-day timeline strip** (cells colored by worst run in that day, today outlined, next-fire ringed in accent), sinks chips with destination inline (slack #ops, file ~/path, webhook host/path), execution chip, hover-revealed actions row (`pause/resume · run now · delete` with confirm), `last run: <status>` summary.
- Run feed (right) — filter chips (`all/succeeded/failed/running`), click row → DetailDrawer.
- DetailDrawer (uses the kanban `DetailDrawer` primitive) — full `output_text`, error in danger pill, per-sink delivery report with timestamp + artifact_ref + error, tokens + cost, prompt-as-fired, `Open session →` button (closes modal and jumps to `execution_session_id`), `cancel run` for in-flight runs.
- New-schedule modal — chip-based "when" suggestions, **live "next 5 fires" preview** (300ms debounce against `previewNextFires`), execution mode as three labelled cards, sinks string field, error surfacing.
- Empty state — Fraunces "Freyja can keep working without you." with three discovery paths (chat / Slack / button).

**Cleanup** (SCH-2 closed)
- Inline `<style>` block deleted. All styling via Tailwind + glass utilities + the canonical palette (`bg-0/[0.96]`, `accent`, `ok`, `warn`, `danger`, `fg-0..fg-3`).
- MissionDashboard scheduler tab removed — was a duplicate mount path that double-stacked a fixed overlay inside a session-scoped frame.
- `bg-bg-0/[0.96] backdrop-blur-[24px]` for the modal surface (matches MissionDashboard pattern).

**Punch list resolution**
- ✅ **SCH-1** — run detail drawer with full output_text, error, delivery_reports per sink, tokens, cost, prompt-as-fired, Open session.
- ✅ **SCH-2** — parallel design system gone; matches the rest of Freyja.
- ✅ **SCH-3** — schedules visualized: 14-day timeline strip + live next-5-fires preview during create.
- ✅ **SCH-11** — tab strip replaced; single page with a focus mode (click a card → drill in).
- ✅ **SCH-12** — schedule cards instead of flat table rows.
- ✅ **SCH-13** — run feed is a card list with status pips, not a dense table.
- ✅ **SCH-15** — sinks chips carry destination inline.
- ✅ **SCH-17** — `/schedule` + aliases + palette + `⌘⇧S` + TitleBar pill.
- ✅ **Live by default** — push events drive the store; polling removed.

**Deferred** (intentionally — would have bloated this pass past the user's review attention):
- SCH-4 (in-flight visualization with `scheduler_run_claimed` live progress beyond the run-feed `running` filter)
- SCH-5 (search across runs by output_text — needs new bridge IPC)
- SCH-10 (permission chip on jobs)
- SCH-14 (per-job cost sparkline) — placeholder geometry exists; sparkline mounts in a follow-up
- SCH-18 (duplicate-a-job)
- SCH-19 (Test now before saving)
- SCH-20 (iteration breakdown in detail drawer — currently shown as "fire # (iter N)")

These are tracked for the next pass.

