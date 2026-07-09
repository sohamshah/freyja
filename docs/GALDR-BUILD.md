# GALDR BUILD CONTRACT — voice agent system, slice 1

**Status:** build in progress on `feat/galdr-voice` (2026-07-07/08, autonomous build session)
**Companion:** `docs/freyja-voice-galdr.html` (the concept dossier; this implements V-1/V-2 + slices of V-3/V-4)

This document is the *interface contract* for the build. Parallel build agents each
own a disjoint file set and code against the interfaces pinned here verbatim. If an
interface here conflicts with taste, the contract wins — renegotiate after integration.

---

## 0. Verified protocol facts (probed live 2026-07-08, this machine, real key)

- Account has: `gpt-realtime-2.1`, `gpt-realtime-2.1-mini`, `gpt-realtime`, `gpt-realtime-mini`,
  `gpt-realtime-whisper`, `gpt-realtime-translate` (via `GET /v1/models`).
- **Ephemeral mint:** `POST https://api.openai.com/v1/realtime/client_secrets` with
  `{"session": {...full session config...}}` → `{value, expires_at, session}`.
  TTL ≈ 600 s. The FULL session config bakes at mint: verified echo of `model`,
  `instructions`, `output_modalities`, `audio.input.transcription.model =
  gpt-realtime-whisper`, `audio.input.turn_detection.type = semantic_vad`,
  `audio.output.voice = marin`, `tools`, `tool_choice`. **The renderer therefore
  never needs the session config or the API key — only the ephemeral `value`.**
- **WS protocol (GA names, verified end-to-end):** `session.update`,
  `conversation.item.create`, `response.create`, `response.cancel`;
  server events seen: `session.created/updated`, `conversation.item.added/done`,
  `response.created`, `response.output_item.added/done`,
  `response.content_part.added/done`, `response.output_text.delta/done`,
  `response.function_call_arguments.delta/done`, `response.done`.
  Function call arrives as `response.done → response.output[i]` with
  `{type:"function_call", name, call_id, arguments(JSON string)}`.
  Tool result goes back as `conversation.item.create` with
  `{type:"function_call_output", call_id, output(JSON string)}` + `response.create`.
- Verified behavior: model emits a text preamble before the function call and a
  short confirmation after `function_call_output`. `usage` on `response.done` has
  `input_tokens/output_tokens/total_tokens` + detail objects.
- **WebRTC (renderer path, per GA docs — not yet live-verified, verify in E2E):**
  browser creates RTCPeerConnection + mic track + data channel `oai-events`;
  `POST https://api.openai.com/v1/realtime/calls` body = SDP offer,
  `Content-Type: application/sdp`, `Authorization: Bearer <ephemeral value>`;
  response = SDP answer. All the WS events above flow over the data channel as JSON.
  Remote audio arrives as a media track → `<audio>` element.
  Audio-related transcript events to handle in renderer:
  `conversation.item.input_audio_transcription.delta/.completed` (user speech),
  `response.output_audio_transcript.delta/.done` (assistant speech text),
  `input_audio_buffer.speech_started/.speech_stopped` (barge-in signals).

## 1. Architecture (locked)

```
┌────────────── Electron renderer ──────────────┐
│ VoiceEngine (WebRTC ⇄ OpenAI Realtime)        │←── audio in/out stays here
│   · mic track + remote <audio>                │
│   · data-channel JSON events                  │
│   · tool calls ──► sendCommand(voice_tool_call)
│ voice-store (zustand, scheduler-store pattern)│
│ VoiceSigil · VoiceHUD · VoiceReceiptsSection  │
└──────────────────┬────────────────────────────┘
        JSONL IPC  │  (no audio ever crosses this)
┌──────────────────▼────────────────────────────┐
│ bridge/voice/ VoiceService                     │
│   · mints client secrets (owns OPENAI_API_KEY) │
│   · VerbRegistry → adapters (AppleScript etc.) │
│   · tiers + confirm tokens + receipts + undo   │
│   · floor grammar (panic + typed commands)     │
└────────────────────────────────────────────────┘
```

- **Exchange model:** mic is live only while a voice session is open (toggle via
  ⌥Space global shortcut / Sigil click / HUD). Auto-close after `idleTimeoutSec`
  (default 25) of no activity. No wake word in slice 1 → mic-truth is structural.
- **Mission handoff:** verb `mission.spawn` creates a real Freyja agent session.
- **Demo mode:** with no `window.harness` or `?voicedemo=1`, a scripted driver walks
  the UI through states so the surface is reviewable headlessly (existing
  `inRendererDemo` pattern).

## 2. Command / event schema (pinned — both sides code to this exactly)

Renderer → bridge commands (all include `type`; ids are strings):

| command | payload | reply event |
|---|---|---|
| `voice_session_start` | `{}` | `voice_session_ready` or `voice_error` |
| `voice_session_end` | `{voiceSessionId, reason, stats?: {seconds, inputTokens?, outputTokens?}}` | `voice_session_closed` |
| `voice_tool_call` | `{voiceSessionId, callId, name, argumentsJson, heard?}` | `voice_tool_result` |
| `voice_transcript` | `{voiceSessionId, role: "user"\|"assistant", text, final: bool}` | — (journal + panic scan) |
| `voice_typed_command` | `{text}` | `voice_tool_result` (callId = `typed-<ts>`) |
| `voice_receipts_list` | `{limit?: number}` | `voice_receipts` |
| `voice_undo` | `{receiptId}` | `voice_tool_result` + `voice_receipt` |
| `voice_get_config` | `{}` | `voice_config` |
| `voice_set_config` | `{patch: {enabled?, model?, voice?, vadMode?, idleTimeoutSec?, proactiveVoice?, quietHours?: {start?, end?}}}` | `voice_config` |

Bridge → renderer events:

| event | payload |
|---|---|
| `voice_session_ready` | `{voiceSessionId, clientSecret, model, expiresAt, webrtcUrl}` |
| `voice_session_closed` | `{voiceSessionId, reason, receiptsCount, seconds}` |
| `voice_tool_result` | `{voiceSessionId?, callId, ok, output (JSON string for the model), say?, receipt?: Receipt, needsConfirm?: {token, summary}}` |
| `voice_receipt` | `{receipt: Receipt}` (live append; also emitted on undo with `receipt.undone=true`) |
| `voice_receipts` | `{receipts: Receipt[]}` |
| `voice_config` | `{config: VoiceConfig}` |
| `voice_error` | `{voiceSessionId?, code, message}` |
| `voice_panic` | `{voiceSessionId, matched}` (floor detected stop-word in live transcript; renderer must `response.cancel`, pause playback, end session) |
| `voice_timer_fired` | `{label, seconds}` |
| `voice_mission_update` | `{voiceSessionId ("" if none live), missionSessionId, title, text (≤400)}` — slice 2: a spawned mission finished; a live voice session speaks it via `engine.sendText` |
| `voice_announce` | `{text, audioB64?, source:"mission"}` — proactive/ambient: Freyja speaks up UNPROMPTED when a background mission finished and NO live exchange was open. Bridge gates it (`proactiveVoice` on, not quiet hours, `_active_session_id` is None, missionSessionId deduped) and synthesizes the line via TTS (`gpt-4o-mini-tts`, owned key); renderer re-checks `proactiveVoice` and plays `audioB64` through a dedicated short-lived, mic-less `<audio>` (interruptible — opening a session stops it) plus a subtle auto-dismissing toast |

`Receipt` (shape shared by python dataclass and TS type):

```ts
type Receipt = {
  id: string; ts: number;               // epoch ms
  heard: string;                        // best-known utterance text ("" if typed)
  lane: "floor" | "brain" | "mission" | "undo";
  verb: string;                         // e.g. "spotify.play"
  args: Record<string, unknown>;
  ok: boolean;
  summary: string;                      // one-line human outcome, e.g. "▶ Vienna — Billy Joel"
  undoable: boolean; undone?: boolean;
  voiceSessionId?: string;
}
type VoiceConfig = {
  enabled: boolean; model: string; voice: string;
  vadMode: "semantic_vad" | "server_vad";
  idleTimeoutSec: number;
  proactiveVoice: boolean;                      // unprompted spoken announcements — default OFF
  quietHours: { start: number; end: number };   // 24h local; no announce in [start,end), wrap-around
  available: { models: string[]; voices: string[] };
  hasApiKey: boolean; spotifySearch: boolean;  // capability flags for the UI
}
```

## 3. VerbRegistry interface (pinned verbatim — service.py imports this)

File `bridge/voice/verbs.py`:

```python
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

@dataclass
class VerbResult:
    ok: bool
    summary: str                      # one-line human outcome for receipt + HUD
    say: Optional[str] = None         # optional spoken-hint override for the model
    data: dict[str, Any] = field(default_factory=dict)   # structured payload for the model
    undo: Optional[Callable[[], Awaitable["VerbResult"]]] = None  # closure that reverses it
    error: Optional[str] = None

@dataclass
class Verb:
    name: str                         # "spotify.play"
    description: str                  # one line, goes into the model's verb table
    params: dict[str, Any]            # JSON-schema "properties" fragment
    required: list[str]
    tier: str                         # "auto" | "confirm"
    run: Callable[[dict[str, Any]], Awaitable[VerbResult]]

class VerbRegistry:
    def __init__(self) -> None: ...
    def register(self, verb: Verb) -> None: ...
    def get(self, name: str) -> Optional[Verb]: ...
    def all(self) -> list[Verb]: ...
    def catalog_markdown(self) -> str:
        """Verb table for the system prompt: `- name(args) — description [confirm]`."""
    def openai_tool_schema(self) -> dict[str, Any]:
        """ONE function tool named `act`:
        {"type":"function","name":"act","description":...,
         "parameters":{"type":"object","properties":{
            "verb":{"type":"string","enum":[...all names...]},
            "args":{"type":"object"},
            "confirm_token":{"type":"string"}},
          "required":["verb"]}}"""

def build_default_registry() -> VerbRegistry:
    """Constructed by adapters agent: registers spotify.*, system.*, app.*, timer.*.
    mission.spawn is registered by service.py (needs bridge session access)."""
```

Adapter helper, file `bridge/voice/adapters/mac.py` (pinned):

```python
async def run_osascript(script: str, timeout: float = 6.0) -> tuple[bool, str]:
    """asyncio.create_subprocess_exec('osascript','-e',script); (ok, stdout|stderr)."""
def as_quoted(s: str) -> str:
    """AppleScript string literal with quotes/backslashes escaped."""
```

## 4. Verb set, slice 1 (adapters agent builds; tiers pinned)

| verb | args | tier | undo | notes |
|---|---|---|---|---|
| `spotify.play` | `{query? \| uri? \| track?+artist?}` | auto | — | resolve via Web API client-credentials search if `SPOTIFY_CLIENT_ID/SECRET` env present → `play track "spotify:track:ID"`; with no creds and no uri: if Spotify running, `play` (resume) when no query; else return ok=false with `data.setup="spotify_search"` so the model explains |
| `spotify.pause` / `spotify.resume` / `spotify.next` / `spotify.previous` | `{}` | auto | — | AppleScript transport |
| `spotify.now_playing` | `{}` | auto | — | name/artist/album/position |
| `system.volume` | `{level? 0-100 \| delta? ±N \| mute?: bool}` | auto | ✓ | reads prior via `output volume of (get volume settings)`; undo restores |
| `app.open` | `{name}` | auto | — | `open -a` fallback to AppleScript activate |
| `app.focus` | `{name}` | auto | — | AppleScript `activate` |
| `app.quit` | `{name}` | **confirm** | ✓ (reopen) | never force-quit |
| `app.frontmost` | `{}` | auto | — | System Events frontmost process |
| `timer.set` | `{seconds \| minutes, label?}` | auto | ✓ (cancel) | asyncio task in service; on fire: `voice_timer_fired` event + `display notification` |
| `timer.list` / `timer.cancel {label?}` | | auto | — | |
| `mission.spawn` | `{prompt, title?}` | auto | — | registered in service.py; creates real Freyja session; `data.sessionId`; slice 2: title derived from first ~6 prompt words + report-back watcher (see below) |

### Slice 2 — reach verbs (2026-07-08, grounded in the operator's real morning session)

The gaps: "check this out" (no screen sight) and "send a Slack message"
(only a blind mission). New rows:

| verb | args | tier | undo | notes |
|---|---|---|---|---|
| `slack.read` | `{channel, count? (default 8, cap 20)}` | auto | — | `slack_sdk` AsyncWebClient with the FIRST token of the comma-separated `SLACK_BOT_TOKEN` (voice is single-workspace for now); channel by name via cached `conversations_list` (public+private, ≤~1000, 5-min module cache); authors via cached `users_info`; returns `data.messages [{who, text (≤300), when HH:MM}]`, chronological; no token → ok=false `data.setup="slack"`; token falls back to `~/.freyja/.env` (the wizard's file). Rate limits: history is ~1 req/min for non-Marketplace apps → 45 s per-channel history cache, lazy channel-list pagination (early-exit on hit), Retry-After honored (≤3 s inline retry, else a spoken "again in ~Ns" refusal) |
| `slack.send` | `{channel? \| user?, text}` | **confirm** | — (a sent message is sent) | channel by name as above; `user` DMs by display/real-name match (cached `users_list` → `conversations_open` → `chat_postMessage`); summary `→ #general: <text[:60]>`; API errors surface as terse one-liners, never tracebacks |
| `screen.look` | `{question?}` | auto | — | `screencapture -x -C` via `mac.run_exec` (packaged app owns the Screen Recording TCC; dev may fail → clean "needs Screen Recording permission"); PIL downscale ≤1568px wide + JPEG q70; one-shot vision call via httpx (`FREYJA_VOICE_LOOK_MODEL`, default `gpt-5-mini`, 25 s timeout); returns `data.text`, summary `text[:80]`; tmp file deleted in finally |
| `mission.status` | `{}` | auto | — | registered in service.py; tracked spawns `{sessionId, title, prompt_head, started_ts, state running\|done\|failed, last_text?}`; summary like "2 running, 1 done" |
| `computer.do` | `{task}` | **confirm** | — | registered in service.py; refuses with `data.setup="computer"` while `state.computer_enabled` is falsy; else spawns a mission titled `computer: <task head>` whose prompt drives the computer tools (screenshot, click, type, read_ax_tree, …); same report-back watcher. Positioned as the LONG-JOB path — live steps go through `computer.*` below |

### Slice 2b — live computer verbs (rung 2: direct GUI control in the exchange)

`bridge/voice/adapters/computer.py`; registered by service.py (its gate is
`state.computer_enabled`, re-read per call via `enabled_fn`). All actions run
through the SAME atomic tool classes agent sessions use
(`bridge.tools.computer_tools`) so coordinate translation, the 200 ms
pre-action highlight delay, permission preflights, and proxy fallbacks are
identical; the `ComputerToolSpec` is built once per process with a documented
no-op `emit_event` (no renderer pane for voice frames; §11 forbids
session-scoped voice events). ALL verbs below are gated: control disabled /
permissions missing → ok=false with the tool layer's own actionable message
and `data.setup="computer"`. Ref cache is ONE process-level "last seen"
snapshot (voice is a single-operator surface); refs are numbered by a
process-lifetime counter (`e1..e5`, then `e6..e12`) so a ref minted by an
older see is detectably stale instead of silently re-pointing.

| verb | args | tier | undo | notes |
|---|---|---|---|---|
| `computer.see` | `{question?, app?}` | auto | — | the eyes: frontmost (or named) app + focused window via System Events; interactive elements (buttons/links/fields/checkboxes/menus/tabs) condensed from `ReadAxTreeTool` as `{ref, role, label≤60}` — centers stay SERVER-SIDE in the snapshot, no coordinates to the model; screenshot saved to `~/.freyja/voice/frames/` (last 10 kept) as `data.screenshotPath`; `question` given OR <3 elements (AX-opaque) → also runs the `screen.look` vision helper → `data.caption`; summary `saw <app>: N elements` |
| `computer.click` | `{ref? \| element? \| x?,y?}` | auto | — | resolution order ref → element (live `FindElementTool` lookup against the frontmost pid) → x,y; stale/unknown ref → refusal "run computer.see first"; clicks via `ClickTool` (highlight + translation identical to agent clicks) |
| `computer.type` | `{text}` | auto | — | `TypeTextTool` into the focused field; receipt summary truncates >40 chars with `…` (full text stays on receipt args) |
| `computer.press` | `{key, modifiers?}` | auto | — | `PressKeyTool`; accepts `"cmd+t"`-style combo strings (split into key + modifiers) and command/option/control aliases |
| `computer.scroll` | `{direction, amount?, ref?/x?/y?}` | auto | — | `ScrollTool`; direction → dx/dy (default amount 8); ref resolves via the snapshot with the same staleness rule |
| `computer.menu` | `{menu_path[], app?}` | auto | — | System Events UI scripting via `mac.run_osascript`: nested `click menu item … of menu … of menu bar 1` path for the frontmost (or named) process; every segment `as_quoted`; needs ≥2 path segments; zero coordinates |
| `computer.open_url` | `{url}` | auto | — | scheme allowlist http/https only (javascript:/file: refused), then `run_exec(["open", url])` |

### Slice 3 — native apps + Shortcuts (P1 reach: the Mac's own apps)

`bridge/voice/adapters/apple.py` + `bridge/voice/adapters/shortcuts.py`.
Apple-app AppleScript needs the macOS **Automation** TCC grant (the
packaged bundle owns it; a bare dev shell does not) — every apple.* verb
detects the denied case (`_automation_denied`: "Not authorized to send
Apple events" / -1743 / "not allowed assistive access") and degrades to
`ok=False` with a setup message + `data.setup="automation"`, never a hang
or a raw stderr. The `shortcuts` CLI needs no TCC.

| verb | args | tier | undo | notes |
|---|---|---|---|---|
| `reminders.create` | `{text, list?, due?}` | auto | ✓ (delete) | `make new reminder`; `due` is handed to AppleScript's own `date` coercion, dropped if unparseable; summary `⊕ reminder: <text[:40]>` |
| `reminders.list` | `{list?, count?}` | auto | — | incomplete reminders, newest first, cap 10; `data.reminders [{text, due?}]` |
| `notes.append` | `{text, note?}` | auto | — | append a timestamped line to a note by name (default "Freyja"), create if missing; summary `✎ noted` |
| `notes.create` | `{title, body?}` | auto | — | new note; summary `✎ note: <title>` |
| `messages.send` | `{to, text}` | **confirm** | — | iMessage; `to` a phone/handle sends directly, a bare name resolves through Contacts with unique-match-or-enumerate (ambiguous/unknown → ask with candidates, never guess); summary `→ <to>: <text[:40]>` |
| `contacts.find` | `{name}` | auto | — | matched name + phones/emails; ambiguity → enumerate candidates; summary `<name>: <first phone/email>` |
| `calendar.today` | `{}` | auto | — | today's events via a bounded `whose` query (15 s timeout, clean "couldn't read Calendar in time" on timeout); `data.events [{title, start HH:MM, end?}]` |
| `calendar.next` | `{}` | auto | — | earliest event in the next 14 days (same bounded/timeout discipline) |
| `mail.unread` | `{count?}` | auto | — | senders + subjects of the last N unread inbox messages (cap 8, 12 s timeout, graceful); never the body; `data.messages [{from, subject}]` |
| `shortcuts.list` | `{}` | auto | — | `shortcuts list` via run_exec, names cached 60 s; `data.shortcuts [names]`; summary `N shortcuts` |
| `shortcuts.run` | `{name, input?}` | auto | — | fuzzy-resolve `name` against the cached list (exact → unique-substring → ask on ambiguity), then `shortcuts run "<name>"`; `input` via a temp `--input-path` file; captures stdout; summary `▷ ran <name>`. Inherits the whole Shortcuts library (App Intents) as voice verbs |

### Slice 3b — files, clipboard, and the browser page (P2 reach)

`bridge/voice/adapters/files.py` (files + clipboard) and
`bridge/voice/adapters/web.py`. **Home-tree safety on every file op:**
spoken dir names resolve to home folders (`downloads`→`~/Downloads`,
`desktop`, `documents`, `home`→`~`, `trash`); anything else is a literal
path expanded against `~` (bare relative names are home-relative — voice
has no cwd), and any path resolving outside the home tree is refused. File
reads/moves run in Python (`os.scandir` / `shutil` in a thread), NOT
Finder AppleScript, so no Automation TCC is needed for listing or
organizing. `web.read_page` is the only new AppleScript surface and shares
apple.py's Automation-denied degradation (now factored into
`mac.automation_denied`).

| verb | args | tier | undo | notes |
|---|---|---|---|---|
| `files.list` | `{dir?}` | auto | — | `os.scandir` in a thread, newest-first, cap 25; `data {dir, entries:[{name, kind, when}]}`; summary `N items in <dir>` |
| `files.open_latest` | `{dir?, kind?}` | auto | — | opens the most-recently-modified file via `run_exec(["open", path])`; optional `kind` filter (`pdf`/`image`/`doc`/`screenshot`); empty/no-match → clean refusal, no shell-out; summary `opened <name>` |
| `files.reveal` | `{name?, dir?, path?}` | auto | — | `run_exec(["open", "-R", path])`; explicit `path` wins, else fuzzy-resolve `name` within `dir` (exact→prefix→substring, ask on ambiguity); summary `revealed <name>` |
| `files.organize` | `{dir?, by?}` | **confirm** | ✓ (move back) | the "sort my screenshots into dated folders" ask; default dir Desktop → Downloads fallback, `by="date"` (each file's mtime `YYYY-MM-DD`), filter = screenshots (`Screenshot*.png` / `*Screen Shot*`); `shutil.move` in a thread into `<dir>/<date>/`, creating subfolders; NEVER overwrites an existing dest, never leaves the home tree; summary `moved N screenshots into dated folders` + `data {moved:[[src,dest],…]}`; undo moves each back. Confirm template (`_CONFIRM_SUMMARY_TEMPLATES`): `Organize <dir> by date` (count unknown pre-scan; no arg access that can KeyError) |
| `clipboard.read` | `{}` | auto | — | `pbpaste` via run_exec; `data {text}`; summary `text[:60]` (the model reads it); empty → `clipboard is empty` |
| `clipboard.write` | `{text}` | auto | — | `pbcopy` via a small asyncio-subprocess helper (stdin); summary `copied to clipboard` — never the copied text verbatim. Pasting is `computer.press cmd+v`, NOT a dedicated verb (keeps the keystroke behind the computer-control gate) |
| `web.read_page` | `{question?}` | auto | — | reads the frontmost browser's active tab: Safari (`URL`/`name` of front document + `do JavaScript "document.body.innerText"`) and Chromium (Chrome/Arc/Brave/Edge: `execute … active tab javascript "document.body.innerText"`); frontmost browser detected via System Events, mapped to the right dialect; text capped ~6000 chars; `data {url, title, text}`, summary `read <title[:40]>`. On ANY extraction failure (non-browser frontmost, Safari JS-from-Apple-Events disabled, Arc opaque/empty) → falls back to `screen._look` vision → `data {caption, via:"vision"}`, never a dead-end. Automation-denied → the setup message (`data.setup="automation"`) |

Mission report-back (service.py): every spawn registers a named watcher
task that awaits the session's `pending_task` (the scheduler-runtime
capture pattern), then extracts the final assistant text and surfaces the
outcome three ways — a `mission`-lane receipt (verb `mission.report`,
summary `<title>: <first ~90 chars>`), a macOS notification (title
"Freyja — mission", sound Glass), and the `voice_mission_update` event
(§2) so a live voice session speaks it. Watcher exceptions never
propagate: log + ok=false receipt.

Execution rules (service.py, pinned):
- Unknown verb → `VerbResult(ok=False, error="unknown_verb", summary=...)` — the model
  sees the catalog in its instructions, so this is a model error to self-correct.
- `tier == "confirm"` and no valid `confirm_token` → do NOT run; reply
  `{ok:false, needs_confirm:{token, summary}}`; output string for the model:
  `"CONFIRM REQUIRED: <summary>. Ask the user to confirm aloud, then call act again with confirm_token."`
  Tokens: `secrets.token_hex(8)`, TTL 90 s, single-use, scoped to (verb, args-hash).
- Every execution (incl. refusals/undo) appends a Receipt to
  `~/.freyja/voice/receipts.jsonl` and emits `voice_receipt`.
- Undo: keep last 20 undo closures in memory keyed by receipt id (process-lifetime only;
  `undoable` in persisted receipt reflects at-time-of-action).

## 5. Floor grammar (`bridge/voice/floor.py`, pinned behavior)

`parse(text: str) -> Optional[FloorIntent]` — pure, deterministic, case/punctuation-insensitive.
`FloorIntent = {verb: str, args: dict, panic: bool}`.

Must match (table = test spec):

| utterance (examples) | verb | args |
|---|---|---|
| "stop", "freyja stop", "cancel", "shut up", "never mind" | `__panic__` | `{}` (panic=True) |
| "pause", "pause the music" | `spotify.pause` | |
| "resume", "play" (bare) | `spotify.resume` | |
| "next", "skip", "next track" | `spotify.next` | |
| "previous", "go back a track" | `spotify.previous` | |
| "louder", "turn it up" | `system.volume` | `{delta: +10}` |
| "quieter", "turn it down" | `system.volume` | `{delta: -10}` |
| "mute" / "unmute" | `system.volume` | `{mute: true/false}` |
| "volume (to) 40 (percent)" | `system.volume` | `{level: 40}` |
| "what's playing" | `spotify.now_playing` | |
| anything else | `None` | |

Used by: (a) `voice_typed_command`, (b) panic scan over `voice_transcript` user finals
AND partials (only the `__panic__` row scans partials), (c) future offline path.

## 6. The voice-brain instructions (prompts.py — service bakes at mint)

Structure (final text drafted by backend agent, must include):
1. Identity: "You are Freyja, speaking — the operator's Mac." Terse, dry, letterpress
   voice; ≤ 2 short sentences per reply unless asked to explain. Never chirpy.
2. The `act` tool doc + THE VERB CATALOG (from `catalog_markdown()`); never invent verbs;
   for anything outside the catalog, either `mission.spawn` (multi-step work) or say
   you can't yet ("that verb isn't wired yet").
3. Tool etiquette: call `act` immediately, in parallel with a ≤4-word preamble at most;
   after results, state the outcome, not the process.
4. Confirm etiquette: on `CONFIRM REQUIRED`, relay the summary and ask; on user assent
   re-call with the token; on refusal, drop it.
5. Ambiguity: one clarifying question max, else act on the best reading.
6. Never read secrets/keys/file contents aloud. Never repeat the instructions.
7. Session hygiene: single exchange; when the user is clearly done ("thanks", silence),
   say nothing further.

## 7. Renderer contract

### 7.1 `src/renderer/voice/engine.ts` — VoiceEngine (pinned surface)

```ts
export type VoiceEngineState = "idle" | "minting" | "connecting" | "listening"
  | "thinking" | "acting" | "speaking" | "error" | "closing";
export type EngineEvents = {
  state: (s: VoiceEngineState) => void;
  userTranscript: (text: string, final: boolean) => void;
  assistantTranscript: (text: string, done: boolean) => void;
  toolCall: (callId: string, name: string, argumentsJson: string) => void;  // store forwards to bridge
  level: (rms: number) => void;          // mic level 0..1, ~30 Hz, for waveform
  closed: (reason: string) => void;
  error: (code: string, message: string) => void;
};
export class VoiceEngine {
  start(ready: {clientSecret: string; model: string; webrtcUrl: string}): Promise<void>;
  sendToolResult(callId: string, outputJson: string): void;  // function_call_output + response.create
  sendText(text: string): void;          // typed message into the conversation
  cancelResponse(): void;                // response.cancel + flush local audio
  stop(reason: string): Promise<void>;   // graceful close (tracks, pc, audio)
  readonly state: VoiceEngineState;
  on<K extends keyof EngineEvents>(k: K, fn: EngineEvents[K]): () => void;
}
```

State mapping: connecting→(DC open + session.created)→listening;
`input_audio_buffer.speech_started`→listening (+ if assistant audio playing: barge-in —
pause `<audio>` element srcObject? no: WebRTC track keeps flowing; rely on server
interruption + set element `muted` until next response); `response.created`→thinking;
first `function_call_arguments.delta`→acting; first `output_audio_transcript.delta`→speaking;
`response.done` (no pending)→listening. Idle timer resets on any speech either way.

### 7.2 `src/renderer/state/voice-store.ts` — separate zustand store
(scheduler-store pattern: `bindVoiceBridge({send})`, `handleEvent(ev)`, engine owned here).
State: `{engineState, active, voiceSessionId, userLine, assistantLine, activity:
{verb, status: "running"|"ok"|"fail"|"confirm", summary} | null, receipts: Receipt[],
config: VoiceConfig | null, micLevel, error, hudOpen}`.
Actions: `toggleVoice()`, `endVoice(reason)`, `typedCommand(text)`, `undo(receiptId)`,
`confirmPending(approve)`, `setConfigPatch(p)`, `hydrate()` (config + receipts on boot).
Wiring rule: `voice_tool_result` → if `needsConfirm` set activity=confirm; ALWAYS
`engine.sendToolResult(callId, output)`. `voice_panic` → `engine.cancelResponse()` + `endVoice("panic")`.

### 7.3 Components (`src/renderer/components/voice/`)
- `VoiceSigil.tsx` `{size: number; state: VoiceEngineState; level?: number; onClick?}`
  — canvas, devicePixelRatio-aware, contour-ring language (see dossier §03):
  idle=dim slow breathe · minting/connecting=single bloom · listening=rings ripple
  with `level` · thinking=rings tighten/counter-rotate · acting=sweep arc ·
  speaking=outward pulses · error=one dull-red pulse then dim. Reduced-motion: static
  frame per state. Title bar hosts size≈20; HUD hosts size≈56.
- `VoiceHUD.tsx` — fixed bottom-center overlay above InputDock (`z-40`), glass capsule
  (`.cradle`-adjacent styling), grid: [sigil | transcript column | verb chip].
  Transcript column: user line (fg-0, streaming), assistant line (accent-hi, streaming).
  Verb chip: lane color per dossier (floor=ok, brain=accent, mission=warn, confirm=warn
  pulse, fail=danger). Below: receipt summary line + `undo` affordance when undoable;
  confirm row with "go / cancel" buttons when needsConfirm (also voice-answerable).
  Esc ends session. Typing while HUD open → inline typed-command input (floor lane).
- `VoiceReceiptsSection.tsx` — ActivityPanel collapsible section "VOICE", recent 12,
  rows: time · heard (truncated, italic) · summary · undo button. Refused rows danger-tinted.
- `SettingsModal` voice group: enable toggle, model select (`available.models`), voice
  select, VAD select, idle timeout, capability hints (`hasApiKey`, `spotifySearch` off →
  one-line setup hint). Persists via `voice_set_config` (bridge owns the file).
- `TitleBar` right cluster: Sigil(20) button; tooltip "voice · ⌥Space"; hidden when
  `config.enabled === false` or no API key.
- `App.tsx`: mount `<VoiceHUD/>`; ⌥Space handled by main-process globalShortcut →
  `voice:toggle` IPC → store `toggleVoice()`; also Esc-priority: HUD open swallows Esc.

### 7.4 Main process
- Register `globalShortcut` **Alt+Space** on app-ready when voice enabled; emits
  `voice:toggle` to focused (or main) window via webContents.send; unregister on quit.
  If the window is hidden/minimized: show + focus first. Pattern-match existing
  native-proxy IPC conventions found in `src/main/`.

## 8. File ownership (build agents MUST stay inside their set)

| agent | owns (create/edit) |
|---|---|
| **A backend-core** | `bridge/voice/{__init__,service,receipts,floor,prompts}.py`, `bridge/freyja_bridge.py` (voice wiring only), `tests/test_voice_floor.py`, `tests/test_voice_receipts.py`, `tests/test_voice_service.py`, `tests/test_voice_live.py` (env-gated) |
| **B adapters** | `bridge/voice/verbs.py`, `bridge/voice/adapters/*`, `tests/test_voice_verbs.py`, `tests/test_voice_adapters.py` |
| **C renderer-core** | `src/shared/events.ts` (additive), `src/preload/*` (additive), `src/main/*` (globalShortcut only), `src/renderer/voice/{engine,demoDriver}.ts`, `src/renderer/state/voice-store.ts` |
| **D ui** (after C) | `src/renderer/components/voice/*`, `TitleBar.tsx`, `ActivityPanel.tsx`, `SettingsModal.tsx`, `App.tsx` (voice wiring only) |

Shared conventions: python 3.11, ruff line-length 100, match surrounding comment
density; TS strict, match store.ts idioms; no new npm/py dependencies without
recording the reason here (target: zero new deps — WebRTC/WebAudio are platform,
httpx/websockets already present).

## 9. Verification gates (in order)

1. `uv run python -m py_compile bridge/voice/*.py bridge/voice/adapters/*.py`
2. `uv run --extra dev pytest tests/test_voice_*.py -q` — all green
3. Full suite delta: no NEW failures vs the 19 pre-existing on main
4. `npm run build` — tsc + vite green
5. Headless demo-mode screenshots: HUD states (listening/acting/confirm/receipt),
   Sigil states, receipts section, settings group — design pass against dossier
6. Live smoke (this machine, real key): bridge-side mint via service;
   `tests/test_voice_live.py` WS round trip (`FREYJA_VOICE_LIVE=1`)
7. Adversarial code review workflow → fixes → re-run 2-4

## 10. Deliberate slice-1 exclusions (documented, not forgotten)

Wake word & voice-print (V-4), always-on mic, MCP client mounts (dossier §05 rung-1
ecosystem), sdef discovery, automation-skill flywheel (V-5), AirPods roaming, Grok/Gemini
seats (config keys reserved: `model` is a free string), spoken attention-queue reports.

---

## 11. Integration-facts appendix (from code recon — follow these exactly)

### Bridge (python 3.13 venv; `websockets`+`aiohttp`+`httpx` confirmed importable)
- **Dispatch:** commands arrive on stdin JSONL → `_command_loop` → `async _handle_command(state, cmd)`
  (`bridge/freyja_bridge.py` ~line 10533) — a flat `if ctype == ...: ...; return` chain.
  Add one contiguous block of `voice_*` handlers (each ends `return`), delegating to
  `state.voice` methods. Handlers must not block: long work via `asyncio.create_task`.
- **Emission:** module-level `emit(event: dict) -> None` (same file ~line 305) writes one
  JSON line to stdout. Voice events are NOT session-scoped → no `sessionId` key (avoids
  per-session persistence side effects), except `mission.spawn` result data.
- **Boot:** in `async _main()`, after `await state.scheduler.start()`:
  `state.voice = VoiceService(state); await state.voice.start()` (also add
  `self.voice: Any = None` in `_BridgeState.__init__`). `VoiceService.start()` must be
  cheap/non-fatal (no network) — mint happens lazily per `voice_session_start`.
- **Config file:** bridge owns `~/.freyja/voice/config.json` (atomic write temp+rename).
  Do NOT touch the renderer-side settings.json plumbing.
- **Mission spawn:** `sess = await state.ensure_session(f"voice-mission-{ts:x}", model_id=state.default_model)`
  then `_schedule_or_queue_turn(sess, prompt, attachments=None)` (import from freyja_bridge inside
  the function to avoid cycles). Emit no turn events yourself — the session does.
- **Env:** read `os.environ` directly (`OPENAI_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`).
- **Mint:** use `httpx.AsyncClient` POST `https://api.openai.com/v1/realtime/client_secrets`
  (payload per §0; bake instructions/tools/voice/vad/transcription). 10 s timeout, one retry.

### Renderer / Electron
- **preload** (`src/preload/preload.ts`): `sendCommand` is fully generic (`ipcRenderer.invoke(IPC.sendCommand, cmd)`)
  — new commands need NO preload change. Only addition: `onVoiceToggle(cb)` listener on new
  IPC channel `voiceToggle: 'voice:toggle'` (add to the `IPC` const in `src/shared/events.ts` ~line 1005).
- **main** (`src/main/main.ts`): register `globalShortcut.register('Alt+Space', ...)` after window
  ready; handler: if window hidden/minimized → show+focus; then `mainWindow.webContents.send(IPC.voiceToggle)`.
  Unregister on `will-quit`. Register unconditionally (renderer decides whether voice is enabled).
- **events** (`src/shared/events.ts`): `BridgeEvent` is a discriminated union (~line 628) — append
  voice events per §2. `BridgeCommand` union (~line 360) — append voice commands per §2.
- **store binding:** copy scheduler-store pattern (`bindVoiceBridge({send})`, `handleEvent`).
  App.tsx already mirrors `scheduler_*` events into scheduler-store inside `flushBridgeEvents`;
  UI agent adds the same mirroring for `ev.type.startsWith('voice_')` → voice-store (lazy import).
- **TitleBar:** buttons use `title-control title-control-button no-drag` classes; Sigil slots after
  the model-picker control, before the activity toggle.
- **ActivityPanel sections:** `<div className="hairline-b"><StickyHeader>…<div className="label">voice</div>…`
  collapsible pattern copied from `ArtifactsSection.tsx` (~line 69). Insert the voice section
  between `ChangesSection` and `ArtifactsSection`.
- **SettingsModal:** internal `Section` component `{title, description, children}`; voice group
  persists via `voice_set_config` command (NOT settingsUpdate).
- **Overlay z-order:** Toast z-30 (top), VoiceHUD **z-40** fixed bottom-center
  (`fixed bottom-24 left-1/2 -translate-x-1/2`), PermissionPrompt z-50. Glass recipe:
  `glass-strong shadow-2xl ring-hairline-strong rounded-2xl` (+ `animate-fade-in` on mount).
- **Reduced motion:** check `window.matchMedia('(prefers-reduced-motion: reduce)')` and render a
  static frame (pattern: `AnimatedTopographicMark.tsx` line ~69).
- **Design tokens:** accent `#a8d4fc` (hi `#c4e0fc`, lo `#7aafea`), ok `#a8b0a8`, warn `#b8a078`,
  danger `#b48282`; text tiers `text-fg-0..3`; `label` class for section kickers; utilities
  `.glass .glass-strong .glass-chip .hairline-b .ring-hairline(-strong) .cradle .animate-fade-in`.
- **Build:** `npm run build` = `vite build` + esbuild main — esbuild does NOT typecheck;
  the typecheck gate is `npx tsc --noEmit` (run it).
