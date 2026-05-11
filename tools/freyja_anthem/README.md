# freyja anthem

Two ~60-second cinematic videos for Freyja, rendered from scratch
with PIL + pure-Python audio synthesis + ffmpeg.

| File                  | What                                                              | Build |
| --------------------- | ----------------------------------------------------------------- | ----- |
| `freyja_icon.mp4`     | 15s breathing icon — the actual contour-line mark, animated, with Departure Mono wordmark. | `build_icon.py` |
| `freyja_anthem.mp4`   | A grounded use-case demo — one mission flows through the board.   | `build.py` |
| `freyja_meta.mp4`     | A meta vision piece — the Fehu rune as anchor, many-mind orbit, glitch beats, AWE typography. | `build_meta.py` |

Both pieces share the Freyja design language — JetBrains Mono,
restrained monochrome with a single accent, paper-textured kanban
cards on a dark gradient, italic "stage direction" narration. No
glow halos, no flashy transitions, no notification-bar chrome.

`build_meta.py` adds surgical YouTube-poop glitch effects (chromatic
aberration, single-frame inversion, hue rotation, horizontal slice
displacement, brightness strobe) at peak moments — Freyja-restrained
most of the time, then breaks open at the climaxes.

## Storyboard

| Time      | Scene           | Beat                                                                                                   |
| --------- | --------------- | ------------------------------------------------------------------------------------------------------ |
| 0:00–0:05 | awakening       | caret blinks; `freyja` types in; subtitle fades in.                                                    |
| 0:05–0:11 | mission         | user prompt types; mission card materializes with `MISSION` stamp.                                     |
| 0:11–0:18 | decomposition   | mission pins to top; four triage cards slide in with hairlines.                                        |
| 0:18–0:24 | specifier       | a dot arcs to each card; checklists type in; cards turn ready.                                         |
| 0:24–0:35 | workers         | narrator: *dispatching explore on …*; cards flip running; tool streams play; heartbeats.               |
| 0:35–0:49 | verification    | cards turn done_unverified, verifier seals or rejects; card_004 round 2; *completion is a promotion*.  |
| 0:49–0:57 | whole_board     | camera pulls back; synthesis cards spawn; *the board runs the work.*                                   |
| 0:57–1:00 | tagline         | `freyja` / *the page writes itself.*                                                                   |

## Audio

Pure-Python synthesis (no numpy needed):

- **Drone** — 4 sine voices (A1, A2, C♯3, A3) with slow LFO. Fades
  in over 4s, holds through the piece, swells in the final 8s.
- **Type clicks** — short noise bursts during text-reveal scenes.
- **Heartbeats** — 60Hz thumps at ~50 bpm during the workers
  scene, with a soft lub-dub double-tap.
- **Verifier chimes** — bell tones (fundamental + 2.4× + 4.1×
  harmonics) on each seal. Lower tone on rejection.
- **Final swell** — clean C5 + G5 under the tagline.

## Running

```bash
python3 tools/freyja_anthem/build.py
```

Output lands at `tools/freyja_anthem/freyja_anthem.mp4`.

### Flags

| Flag              | Use                                                        |
| ----------------- | ---------------------------------------------------------- |
| `--fast`          | renders at 1280×720 for quick previews.                    |
| `--no-audio`      | silent video (skips ~10s audio synthesis).                 |
| `--frames-only`   | renders PNGs only; skips audio + ffmpeg combine.           |
| `--start-frame N` | render starting at frame N (zero-indexed).                 |
| `--end-frame N`   | stop just before frame N. Useful for sampling.             |

Sample one frame:

```bash
python3 tools/freyja_anthem/build.py --start-frame 1200 --end-frame 1201 --frames-only
```

Per-frame PNGs live in `tools/freyja_anthem/frames/` while rendering;
they're regenerated each run.

## Editing

Each scene is a free function `scene_*(img, t, dur)` keyed in the
`SCENES` list at the bottom of `build.py`. Adjust the timing by
changing `Scene(name, start, duration, draw_fn)` entries. Card text
lives in the module-level lists at the top of each scene block:
`MISSION_TITLE`, `MISSION_PROMPT`, `CHILD_CARDS`, `WORKER_TOOL_STREAMS`,
`VERIFY_TOOLS`, `REJECT_TOOLS`, `ROUND2_TOOLS`.

`draw_paper_card` is the master card primitive. It owns the per-state
material (triage paper, ready paper, dark running, amber done_unverified,
green done, red failed), the title-fit shrink, the heartbeat pulse,
the verifier byline, and the rejection callout.

## Why pure Python

The render needs no native deps beyond ffmpeg and JetBrains Mono.
That makes it portable across mac/linux dev boxes without a wheel
build step. PIL handles all drawing; the only "heavy" op is the
GaussianBlur for card shadows.

Audio is synthesized as raw 16-bit PCM into a Python list, then
written via the stdlib `wave` module. ~60s of mono 44.1kHz is
~5MB of intermediate data — well within `list` ergonomics.

## Output details

- 1920×1080, 30 fps, H.264 (libx264, CRF 18, preset slow)
- AAC audio @ 192k
- File size typically ~3–8 MB (dark visuals compress well)

## v2 storyboard (build_meta.py)

| Time      | Scene       | Beat                                                                                       |
| --------- | ----------- | ------------------------------------------------------------------------------------------ |
| 0:00–0:04 | summon      | rune draws itself stroke-by-stroke in the void                                             |
| 0:04–0:10 | orbit       | many minds orbit the rune; pace accelerates; *many minds · one anchor*                      |
| 0:10–0:14 | burst       | rune shatters into card-fragments; strobe + RGB-split; a wall of cards materializes        |
| 0:14–0:20 | cycle       | one hero card rapidly cycles every state; 80 background cards in counterpoint              |
| 0:20–0:26 | chorus      | 9-cell grid of agent types, each running its own tool stream                               |
| 0:26–0:32 | galaxy      | 600-particle spiral with the rune emerging in negative space                                |
| 0:32–0:38 | awe         | huge FREYJA reveal over cascading system events; *a system that thinks at scale*           |
| 0:38–0:44 | pageful     | "the page writes itself." types itself three times in offset echoes                        |
| 0:44–0:52 | reassembly  | particles stream inward; rune brightens; "freyja" types at the bottom                       |
| 0:52–0:55 | silence     | rune fades to black                                                                        |

## v2 glitch director

`glitch_director(img, frame_idx, t)` post-composites surgical effects
at specific frames:

- **frame 300–302** (burst hit): brightness strobe + RGB chromatic-
  aberration split
- **frame 303**: single-frame negative invert
- **frames 420–600** (cycle scene): brief invert + RGB-split every
  18 frames so the cycling cards crackle
- **frames 600–780** (chorus): light RGB shimmer every 30 frames
- **frames 780–960** (galaxy): occasional hue-rotation pulse
- **frames 1000–1001** (AWE peak): strobe + heavy RGB-split
- **frames 1140–1320** (pageful): horizontal slice displacement every
  40 frames — datamoshing-feel
- reassembly + silence: clean — no glitch

The rest of the runtime is pure Freyja restraint.
