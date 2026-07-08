# Galdr — voice agent for Freyja (slice 1)

Speak to your Mac. Freyja opens a realtime voice exchange, the model calls
verbs that run natively on the machine, and every action lands as an
undoable receipt. Push-to-talk (⌥Space); the mic is live only while the
sigil is lit.

![HUD demo](galdr-assets/hud-demo.gif)

This implements phases V-1/V-2 of the concept dossier (`docs/freyja-voice-galdr.html`)
plus slices of V-3/V-4. Full interface contract: `docs/GALDR-BUILD.md`
(protocol facts §0 were live-probed against the real Realtime API before
any code was written).

## What it does

- **⌥Space** (or the title-bar sigil) opens a voice exchange. Say *"play
  Vienna by Billy Joel on Spotify"*, *"quieter"*, *"quit Slack"*, *"set a
  timer for ten minutes"*, *"what's playing"*. The mic closes on silence
  (idle timeout) or Esc — it is never hot in the background.
- The **actuator** is a single `act` meta-tool. The model picks a verb
  from a catalog baked into its instructions; the bridge dispatches it
  through a `VerbRegistry` to native adapters (AppleScript / `open` /
  Core Audio via osascript / async timers). Slice-1 verbs: `spotify.*`
  (transport + optional search), `system.volume` (undoable), `app.open/
  focus/quit/frontmost`, `timer.set/list/cancel`, `mission.spawn`.
- **Two-tier safety.** `auto` verbs run immediately; `confirm` verbs
  (quit an app) require a single-use, 90-second, args-scoped token — the
  model must relay the ask and re-call after you say yes, out loud.
- **Every action leaves a receipt** — `~/.freyja/voice/receipts.jsonl`,
  surfaced live in the HUD and the Activity panel's VOICE section, with
  one-click **undo** (volume restores, quit reopens, timer cancels).
- **The floor.** Panic words (*"stop"*, *"never mind"*) and deterministic
  transport commands are parsed locally and never touch the model — the
  panic scan runs over partial transcripts so *"stop"* cancels mid-sentence.
- **mission.spawn** hands multi-step work to a real Freyja agent session.

## Architecture

Audio stays in the renderer; the bridge never sees a byte of it.

```
renderer  VoiceEngine ⇄ OpenAI Realtime (WebRTC, mic + remote audio)
          │ tool calls + transcripts only, over JSONL IPC
bridge    VoiceService — mints ephemeral secrets (owns OPENAI_API_KEY),
          VerbRegistry → native adapters, tiers + tokens + receipts + undo
```

The ephemeral client secret bakes the entire session config server-side
(model, instructions, verb schema, voice, VAD, transcription), so the
renderer receives only a ~10-minute token — **the API key never leaves the
bridge**.

## Surfaces

| | |
|---|---|
| `VoiceSigil` | Canvas contour-ring mark; its motion maps 1:1 to engine state (listening ripples off real mic RMS, acting sweeps an arc, speaking pulses outward). Reduced-motion → one static frame per state. |
| `VoiceHUD` | Bottom-center glass capsule: streaming transcripts, lane-colored verb chip, confirm row (go/cancel, also voice-answerable), receipt + undo, inline floor-command input. |
| `VoiceReceiptsSection` | Activity-panel history with undo affordances. |
| Settings → *voice · galdr* | Enable, model, voice, turn-detection, idle timeout, capability hints. |

![acting](galdr-assets/hud-acting.png)
![confirm](galdr-assets/hud-confirm.png)
![settings](galdr-assets/settings-voice.png)

## Verification

- **155 voice unit tests** green (floor grammar table, receipts/undo,
  adapters via mocked osascript, service dispatch + confirm cycle + panic).
- Full suite: **12 pre-existing failures, zero new** (kanban/image baseline).
- `tsc --noEmit` and `npm run build` green.
- **Live, end-to-end, against the real API on this machine:** ephemeral
  mint → `gpt-realtime-2.1-mini` → `act` function call → real bridge
  dispatch → native verb → receipt → spoken confirmation, including a
  set-timer / cancel-timer round trip and an undo.

## Config

- `OPENAI_API_KEY` (required) — the voice brain.
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` (optional) — play-by-name
  search; without them, transport verbs and URI play still work.
- `~/.freyja/voice/config.json` — model/voice/VAD/idle-timeout, bridge-owned.

## Deliberately out of slice 1

Wake word, voice-print, always-on mic (V-4); MCP-client verb mounts and
the automation-skill flywheel (dossier §05 / V-5); non-OpenAI voice seats
(the `model` field is a free string, so a Grok/Gemini seat is a config
change once an adapter exists).
