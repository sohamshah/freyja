# Galdr on GPT-Live — migration plan

*2026-09-28. Supersedes the August "no switch" call (GPT-Live was
consumer-only then). Contract doc: `docs/GALDR-BUILD.md`.*

## Verdict: switch, keep realtime as a fallback seat

GPT-Live (`gpt-live-1`, API GA 2026-09-11) splits the voice agent in two:
a **full-duplex voice layer** that listens and speaks at the same time,
and a **delegated backend** — any Responses model — that does the
reasoning and the tool calls. That split is exactly the fix for most of
the Aug 28 autopsy, which traced the bad session to one small realtime
model doing everything at once:

| Autopsy root cause | Realtime 2.1-mini today | GPT-Live |
|---|---|---|
| Talks over you / barge-in | VAD + truncate we never wired | full duplex, native |
| Verb selection beaten by eyes, denies its own reach | mini voice model picks from a 50-verb enum | `gpt-6.1-sol` picks from 49 typed function tools |
| Visual loop (see → click) | screenshots in the voice context, pruned to one | screenshots go to the backend (`function_call_output` with `input_image`) |
| Session length | 60 min | 2 h (`expires_at` − start = 7200 s) |
| Cost | ~$10/$20 per M audio tokens, context re-billed per turn | $0.05/min voice + backend tokens (~$0.001–0.02/turn on sol) |

Measured on our key today (TTS-generated speech over the WS transport,
backend answers a `see_screen` tool with a PNG):

| Backend | end of speech → answer text | notes |
|---|---|---|
| `gpt-6-luna` | 2.2 s | $0.10/$0.50 per M |
| `gpt-6-sol` | 2.3 s | $2/$10 per M |
| `gpt-6-astra` | 4.7 s | slower; τ-Voice leader; $10/$50 per M |
| `gpt-6.1-sol` | — | added 2026-09-29; 2.3–2.7 s to read a screenshot with all tools loaded (sol: 3.0 s); $2/$10 — **default** |

The voice layer covers backend latency itself ("Let me check…") and
backchannels mid-utterance. Confirm tokens survive across delegations —
"Quit Slack" → CONFIRM REQUIRED → "yes, go ahead" → the backend re-called
`app_quit` with the token (probe 2026-09-28).

**Why not Gemini 3.8 Live** (Sept 15, tops the S2S index, takes video):
audio+video sessions cap at 2 min, the base model scores 30% on τ-Voice
(Extended Thinking 68.6% ≈ GPT-Live-Astra 67.9%), and it's a new vendor,
key, and WebSocket PCM transport in the renderer. Watch item, not a move.

## Protocol facts we build on (all verified live)

- **WebRTC create**: `POST /v1/live/sessions` with the *API key*,
  JSON `{session: {...}, transport: {type: "webrtc", sdp: <offer>}}` →
  201 `{session: {id}, transport: {type: "webrtc", sdp: <answer>}}`.
  No ephemeral secret: the SDP offer goes renderer → bridge → OpenAI, the
  key never leaves the bridge. Don't send `session.start` after it.
- Data channel `oai-events`; wait for `session.started` before sending.
- Session object is strict: `model`, `instructions`, `audio.output.voice`,
  `delegation`, `input` (seed messages). Only `delegation.responses` can
  change later (`session.update`); instructions grow via
  `session.instructions.append`.
- **Responses delegation**: backend function calls arrive as
  `response.event` envelopes wrapping `response.output_item.done`
  (`item.type == "function_call"`). Answer each with `response.item.create`
  (`function_call_output`), then one `response.create`. `output` may be an
  array of `input_text` / `input_image` parts — that's the screenshot path.
- Transcripts: `session.input_transcript.delta` /
  `session.output_transcript.delta` — fragments, not turns; sides interleave.
- `session.commentary.append` (spoken, ≤500 tokens) — mission updates.
- `session.usage.updated` — cumulative seconds (~1/min). Backend tokens
  come in nested `response.completed`.
- Clock runs while idle or muted → close idle sessions (`session.close`).
- The voice layer takes no images.

## Architecture

```
renderer (VoiceEngine)                 bridge (VoiceService)            OpenAI
 mic ─┐  RTCPeerConnection ────────── audio (media track) ─────────────▶ gpt-live-1
      └ createOffer ─ voice_live_connect{sdp} ─▶ POST /v1/live/sessions ─▶ (key stays here)
        ◀─────────── voice_live_answer{sdp} ◀──────────────────────────┘
 oai-events DC ◀── response.event{function_call} ◀─────────────────────── backend gpt-6.1-sol
   LiveProtocol ── voice_tool_call ─▶ tier gate → verb → receipt
   LiveProtocol ◀─ voice_tool_result{output, imageB64} ◀─┘
   └ response.item.create{function_call_output[text, image]} + response.create ─▶
```

Unchanged: VerbRegistry, tiers + confirm tokens, receipts + undo, floor
grammar, panic, transcripts journal, missions, HUD, session projection.

## Execution

1. **Bridge** — `model: "gpt-live-1"` default + `liveBackend` config
   (`gpt-6.1-sol` | `gpt-6-luna` | `gpt-6-astra`). `voice_session_start`
   skips the mint for live and emits `voice_session_ready{transport:"live"}`.
   New `voice_live_connect` → `POST /v1/live/sessions` → `voice_live_answer`.
   `VerbRegistry.responses_tools()` projects one function tool per verb
   (`spotify.play` → `spotify_play`; confirm-tier verbs gain an optional
   `confirm_token`). `handle_tool_call` resolves per-verb names alongside
   `act`. Prompts split: `build_live_instructions()` (voice layer: persona,
   delegate everything, never claim unconfirmed effects) and
   `build_backend_instructions()` (verbs, visual loop, honesty, confirm).
2. **Renderer** — `live-protocol.ts`: pure event logic (tool loop with
   batched `response.create`, image outputs, transcript turn segmentation
   by silence gap, state machine, usage). `VoiceEngine.start` takes a
   `live` transport whose SDP exchange is a bridge round trip; `stop`
   sends `session.close` first. `announce()` routes mission updates to
   `session.commentary.append`. Pricing: seconds × $0.05/min + backend tokens.
3. **Settings** — model list gains `gpt-live-1`; backend picker when live.
4. **Verification** — unit tests both sides; gated live test
   (`FREYJA_VOICE_LIVE=1`) for create/tool/image round-trip; headless
   Chrome e2e driving the real engine over real WebRTC with a fake mic.

## What the e2e run found (2026-09-28)

`scripts/e2e_galdr_live.py` drives the real `VoiceEngine` in headless
Chrome over real WebRTC, with the real `VoiceService` answering SDP and
tool calls. Findings folded into this cut:

- **Data-channel ceiling.** GPT-Live's SDP advertises a 1 GiB
  `max-message-size`, but Chromium caps a message at 256 KiB. A 273 KiB
  base64 PNG failed to send, the function output never arrived, and the
  backend stalled ("submit the pending function call outputs"). The
  realtime seat had the same latent ceiling. Fix: the bridge re-encodes
  oversized screenshots as JPEG at the same pixel size (`_fit_image`,
  `imageMime` on the event), and `LiveProtocol` drops an image that still
  won't fit rather than dropping the result.
- **`screen.look` removed from the live tool list.** Given both tools, the
  backend called `screen_look` (a second vision model) before
  `computer_see` every time: one wasted round per look. A vision backend
  reads the screenshot itself.
- **Voice-layer prompt.** The realtime prompt's "say nothing further when
  the operator is done (… silence)" reads badly for full duplex, where the
  operator is always silent while the backend works; the live prompt now
  says to relay every result the moment it lands. (Defensive — the silent
  results seen in the harness turned out to be the next bullet.)
- **Harness gotchas.** Headless Chrome's `getUserMedia` hangs on macOS
  (it asks for mic permission even with the fake device), so the harness
  feeds a WebAudio stream. That stream must carry a noise floor: after
  seconds of perfect digital silence the voice stopped relaying results.
  A real mic never sends digital zeros.

Measured, "what's on my screen" (full production config, `gpt-6-sol`;
`gpt-6.1-sol`, which landed on main 2026-09-29, reads the screenshot in
2.3–2.7 s vs 3.0 s at the same price and is now the default):
delegation ≈1.3 s after speech ends, "Looking." ≈0.5 s later, answer
starts ≈5–6 s after speech ends.

Run it (read-only questions only; verbs really execute):

```sh
uv run --extra dev --with aiohttp python scripts/e2e_galdr_live.py "What app do I have in front right now?"
uv run --extra dev --with aiohttp --with pillow python scripts/e2e_galdr_live.py --fake-screen "What's on my screen right now?" 24
npx tsx test-live-protocol.mjs         # LiveProtocol unit tests
FREYJA_VOICE_LIVE=1 uv run --extra dev pytest tests/test_voice_live.py -k gpt_live
```

## Multi-monitor + prompt pass (2026-09-29)

The Sep 29 session (voice-8ea07ebb44d1; it ran on a pre-GPT-Live build,
realtime-2.1-mini) shows the gap: asked about three screens, the model
said "I only see one screen at a time", told the operator to switch focus
so it could look, re-looked at the same display when asked for "each
monitor", confused "frontmost app is Arc" with what the main display
showed, answered a mis-heard Russian fragment, and gave a vague answer to
"what model are you". Root cause beyond the model: every voice screenshot
was the primary display (computer.see / screen.look passed no display),
and nothing told the model the other monitors existed.

What changed:

- **computer.see** takes `display`: an id, or `"all"` for one look-only
  overview tiling every monitor as it physically sits, each labeled
  ("Display 3 · above-left of the laptop screen"). Without it, it looks
  at the display holding the named/front app's window. Every look
  reports the display list. The display looked at becomes the click
  space (screenshots after a click follow it too).
- **Clicks on non-primary displays.** Input events use one global space
  (primary at (0,0), the monitors above this laptop at y = -1080), but the
  shared computer tools mapped screenshot pixels by scale only — a click
  read off display 2 landed on the laptop. `ComputerToolSpec.native_origin`
  now carries the captured display's corner (read from CoreGraphics via
  ctypes; the native module exposes no position); api<->native, AX-tree
  bounds and the cursor overlay all use it. Agent sessions get the fix too,
  and `list_displays` now prints each display's position.
- **Voice prompt**, restructured on OpenAI's GPT-Live prompting guide:
  role + identity (names GPT-Live and the backend), tone, English-only,
  backchannel / interruption / silence-and-noise policies, delegation
  ("delegate before answering", "never guess while waiting", reuse only
  still-current results), a Screens section (several monitors; never say
  you can see only one), and Limits ("never invent a limitation", "never
  ask the operator to do what the backend can", "frustrated → act, don't
  explain").
- **Backend prompt** gets the live display layout and when to use
  `display="all"` vs `display=<id>`; the realtime prompt gets a short
  multi-monitor note.

Verified with `--fake-desk` (synthetic three-display desk shaped like the
real one, real computer.see display code): "what do you see on each of my
screens" → one `computer_see(display="all")` → per-monitor answer by
position; "look at my top right monitor" → `display="2"`; "just tell me
what's on my other screens, not the laptop" → the two monitors, tersely.
"What model are you?" → "Freyja, your voice, on GPT-Live. The brain
behind me is gpt-6.1-sol."

## Follow-ups (not in this cut)

- Seed `input` with preferences + the last exchange's tail (autopsy:
  "no memory").
- Sideband attach (`/v1/live/sessions/{id}/attach`) so tool calls run
  bridge-side without the renderer relay.
- `session.input_audio.mute` for push-to-talk; AGC/noise tuning for
  full-duplex in a noisy room (AGC is already off for live).
- A spoken confirm round trip in the e2e (the harness plays one
  utterance; the confirm cycle is covered by a WS probe + unit tests).
- Journal `backendText` alongside the spoken lines, so the transcript
  keeps what the backend actually said, not just the paraphrase.
