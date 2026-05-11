# freyja anthem

A ~60-second cinematic teaser for Freyja, rendered from scratch with
PIL + pure-Python audio synthesis + ffmpeg. The output is
`freyja_anthem.mp4` (1920×1080, 30fps, AAC stereo).

The piece is grounded in Freyja's design language — JetBrains Mono,
restrained monochrome with a single accent, paper-textured kanban
cards on a dark gradient, italic "stage direction" narration. No
glow halos, no flashy transitions, no notification-bar chrome.

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
- File size typically ~25–40 MB at default settings
