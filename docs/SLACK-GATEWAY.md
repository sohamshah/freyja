# Slack Gateway — Implementation Plan

> Status: Draft · Author: Soham + Claude (assistant) · Date: 2026-05-27
>
> Focused scope: get Freyja talking on Slack with a `freyja setup`-style
> provisioning flow that ends with the operator chatting with Freyja in
> their workspace. Grounded in a primary-source deep-dive of
> `~/work/services/hermes-agent` and the current Freyja architecture.
> Companion doc: `docs/ALWAYS-ON-PLATFORM.md` (which this is the
> narrowest possible first slice of).

## TL;DR

Freyja today is Electron-locked. We're going to add: (1) a **background gateway daemon** that survives Electron quit, (2) a **Slack platform adapter** using Socket Mode (no public URL required), (3) a **`freyja setup slack`** CLI wizard that generates a Slack app manifest, walks the operator through pasting tokens, and installs + starts the gateway as a launchd service. Result: operator types `freyja setup slack`, takes 5 minutes, opens Slack, DMs the Freyja bot, and gets a streaming response from the same agent engine the desktop app uses — with full goal-mode / sub-agent / memory capabilities available via slash commands and natural language.

The architecture is a strict subset of the production-grade plan in `docs/ALWAYS-ON-PLATFORM.md`. We're shipping the daemon piece and the messaging-gateway piece together because Slack requires both. Workflows, schedules, triggers, capability policy beyond a simple Slack default — all deferred to the broader phased plan. This doc captures the Slack-specific moves only.

## 1. What we're building

Concrete operator experience, end to end:

```
$ freyja setup slack

Welcome to Freyja Slack setup.

Step 1 — Create the Slack app
  1. Open https://api.slack.com/apps?new_app=1
  2. Choose "From an app manifest"
  3. Pick your workspace
  4. Paste this manifest (we've copied it to your clipboard):

     <... full manifest YAML ...>

  Press Enter when the app is created.

Step 2 — Enable Socket Mode + get app token
  1. In your new app, go to Settings → Socket Mode → Enable
  2. Create an App-Level Token with scope `connections:write`
  3. Copy the token (xapp-...)

  Paste your App Token here: xapp-...

Step 3 — Install to workspace + get bot token
  1. Settings → Install App → Install to Workspace
  2. Authorize
  3. Copy the Bot User OAuth Token (xoxb-...)

  Paste your Bot Token here: xoxb-...

Step 4 — Test the connection
  Connecting to Slack... ✓ Authenticated as @freyja in workspace Acme
  
Step 5 — Install the gateway daemon
  Install Freyja gateway as a launchd service (starts at login, runs in
  background)? [Y/n] Y
  ✓ Installed to ~/Library/LaunchAgents/co.freyja.gateway.plist
  ✓ Gateway started (pid 12345)

Setup complete. Open Slack, find @freyja in your DMs, and say hi.
```

Then in Slack:

```
[ DM with @freyja ]

soham    hey, what can you do?

freyja   I'm your Freyja agent. I can help with coding, research,
         writing — pretty much anything you'd ask a capable assistant.
         A few specifically interesting things I can do here on Slack:
         
         • /goal <objective>  — kick off a goal loop with an autonomous
            judge that reviews my work each turn
         • /mode bus|goal|kanban  — switch coordination strategies
         • @mention me in a channel and I'll spin up sub-agents to
            research a topic in parallel and report back in the thread
         • drop me a file (image, code, doc) and I'll use it
         
         What are you working on?

soham    can you write me a small async http client with retry?

freyja   [streams response in-place, edits the message as text grows...]
```

This is the bar.

## 2. What Hermes does (load-bearing pieces only)

The deep dive surfaced the canonical pattern. Compressed to the parts we'll mirror:

### 2.1 Socket Mode over Webhooks

Hermes uses Slack's **Socket Mode** via the `slack-bolt` Python library — specifically `AsyncSocketModeHandler`. This is THE key choice for end-user deployment: all connections are outbound (gateway → Slack), no inbound HTTP server, no public URL, no ngrok, works behind any NAT or corporate proxy. We are absolutely doing the same.

Implementation file to mirror: `~/work/services/hermes-agent/gateway/platforms/slack.py` (line 506–700 for connection, 1767–2050 for message handling).

### 2.2 Two tokens

- **Bot Token** (`xoxb-...`) — for sending messages, reading channels, downloading files. Standard OAuth bot token.
- **App Token** (`xapp-...`) — only for Socket Mode WebSocket. Granted `connections:write` scope.

Both stored in `.env` as plaintext. Hermes does not encrypt them; we won't either in v1 (file lives in `~/.freyja/.env` with default umask).

### 2.3 Manifest-driven app provisioning

Hermes does NOT call any Slack API to create the app. Instead it generates a JSON manifest with all the right scopes / event subscriptions / slash commands and tells the user to paste it into Slack's "Create app from manifest" flow. This is dramatically simpler than OAuth + admin install + the dance of provisioning programmatically. We mirror it exactly.

Hermes's manifest declares:
- `app_mention`, `message.im`, `message.channels`, `message.groups` event subscriptions
- All bot scopes needed (`app_mentions:read`, `chat:write`, `files:read`, `files:write`, `im:history`, `im:read`, `im:write`, `groups:history`, `groups:read`, `channels:history`, `channels:read`, `commands`, `users:read`)
- All slash commands the gateway supports (dynamically generated)
- `socket_mode_enabled: true`

### 2.4 Daemon process model

Gateway is launched by **launchd** on macOS (systemd on Linux, Task Scheduler on Windows). Independent of any CLI or desktop app. PID lock at `~/.hermes/.gateway.pid` prevents duplicate instances. The CLI runs `launchctl load` on first install; afterwards the daemon starts at user login automatically.

`hermes gateway run` runs it in foreground (dev). `hermes gateway service install` installs the plist. `hermes gateway stop` stops it.

### 2.5 Session keying

Hermes maps Slack messages to agent sessions via a structured key:

```
agent:main:slack:dm:<chat_id>                      # 1:1 DM with the bot
agent:main:slack:dm:<chat_id>:<thread_id>          # threaded DM
agent:main:slack:channel:<chat_id>:<thread_id>     # channel thread (shared)
agent:main:slack:channel:<chat_id>:<user_id>       # channel mention (per-user)
```

By default threads are **shared** across all participants (no `user_id` appended), DMs are per-conversation, channel mentions are per-user. This is exactly the right shape — every Slack interaction surface gets a sensible default.

### 2.6 Streaming responses via progressive edits

Slack doesn't have a typing indicator for bots. Hermes uses the alternative: **send the first chunk immediately, then edit the message in place** as more tokens arrive. The `GatewayStreamConsumer` batches deltas every ~500ms and calls `chat.update` (`adapter.edit_message`) to grow the visible message. Operator sees a continuously-growing response, just like in the Freyja desktop app.

### 2.7 Mention gating

In DMs: bot responds to every message.
In channels: bot only responds if `@mentioned` in the message text.
In threads: bot tracks "threads I've been mentioned in" and auto-responds to subsequent thread messages without re-mention.

### 2.8 Slash commands

Hermes registers every CLI command as a native Slack slash command via the manifest. `/stop`, `/model`, `/new`, `/reset`, `/queue`, `/q`, etc. Responses are **ephemeral** (visible only to the issuer) by default. We'll do the same with Freyja's command set.

### 2.9 File handling

Incoming: bot downloads files via `files.info` + the `url_private` (auth required). Cached locally and passed to the agent as attachments.

Outgoing: bot uploads via `files.uploadV2` for images, voice, documents.

### 2.10 Multi-workspace

Hermes supports running one daemon talking to N Slack workspaces via comma-separated `SLACK_BOT_TOKEN` env var. Each workspace gets its own `team_id` → `WebClient` mapping. Sessions are keyed including workspace identity so they don't mix.

## 3. Current Freyja state (gap analysis)

What Freyja has that the Slack gateway can plug into:

- **Multi-session bridge already works** — `_BridgeState.sessions: dict[str, _BridgeSession]` (`bridge/freyja_bridge.py:4996`). Each session runs its own agent turn loop concurrently.
- **Per-session messaging queue** — `_schedule_or_queue_turn` (`freyja_bridge.py:5393`). Messages enqueue if the session is busy; drain via `_run_turn_queue`. We get this for free.
- **Streaming infrastructure** — `AsyncAgentRunner` emits `text_delta`, `thinking_delta`, `tool_use_start` stream events. The gateway adapter just needs to consume these and translate to Slack `chat.update` calls.
- **Sub-agents, judge, calibrator, memory, skills** — all reusable from any session, regardless of the input source.
- **Coordination strategies** (`bus`, `goal`, `kanban`, `isolated`) — also session-scoped; each Slack session gets to pick one.
- **Permission tiers** (`bridge/freyja_bridge.py:1121`) — coarse but functional; we'll use `medium` as the Slack default.
- **Tool registry** — `bridge/tools/registry.py`; supports tool include/exclude per session.

What Freyja DOESN'T have:

- **Daemon mode.** Bridge dies on Electron quit (`src/main/main.ts:676`).
- **CLI entry point.** No `freyja` command; everything is Electron-launched. Need a separate Python entry point that can be run from the shell.
- **Platform adapter abstraction.** Today there's exactly one input source: stdin from Electron. Need a `BasePlatformAdapter` Protocol so we can add Slack now and Telegram/Discord later without rewriting.
- **External session keying.** Today sessions are identified by an opaque ID generated by the renderer. We need a structured key scheme so the same Slack conversation always lands in the same Freyja session.
- **A native notification path back to Slack.** When the operator's desktop renderer fires an action that should surface to Slack, there's no current path. (Probably fine for v1: each Slack session is one-way; agent only speaks to Slack, doesn't initiate from elsewhere.)
- **Setup wizard.** Today configuration is per-session via the renderer; there's no "install + provision external service" flow.

The gap is real but bounded. The agent engine is fully reusable; the new code is a thin (~1500-2000 LOC) gateway layer + Slack adapter + CLI wizard.

## 4. Architecture

Five components. Two are minor changes to existing code; three are new.

### 4.1 Gateway daemon (NEW)

A long-running Python process that:
- Hosts the existing `_BridgeState` + `_BridgeSession` machinery
- Listens for messages from platform adapters (initially: Slack)
- Routes each message to the appropriate session (looked up by structured key)
- Streams responses back through the originating adapter
- Survives Electron quit
- Launched via launchd at user login

Entry point: `bridge/gateway/run.py:main()` — separate from `bridge/freyja_bridge.py:main()` (which stays as the Electron-spawned subprocess for now; eventually gateway replaces it).

Key design choice: **one shared process, not orchestrator + engine pool**. v1 is single-process for simplicity. We're not doing the full production-grade split here — that's deferred to the `ALWAYS-ON-PLATFORM.md` plan. The gateway is a glorified bridge that listens on more than one input source.

PID lock at `~/.freyja/.gateway.pid` so the launchd service doesn't conflict with a manual foreground run.

### 4.2 Platform adapter Protocol (NEW)

```python
# bridge/gateway/platforms/base.py

from typing import Protocol, runtime_checkable, Any
from dataclasses import dataclass, field
from enum import Enum

class Platform(Enum):
    SLACK = "slack"
    # Telegram, Discord, etc. later

@dataclass
class MessageSource:
    """Structured identifier for the conversation a message belongs to."""
    platform: Platform
    workspace_id: str               # Slack team_id, Discord guild_id, etc.
    chat_type: str                  # "dm" | "channel" | "group" | "thread"
    chat_id: str                    # Slack channel_id (C... / D... / G...)
    user_id: str | None = None      # Slack user_id (U...)
    user_name: str | None = None    # Display name
    chat_name: str | None = None    # Channel name (#general) or DM partner name
    thread_id: str | None = None    # Slack thread_ts
    message_id: str | None = None   # Slack ts (for in-place edits)

@dataclass
class IncomingMessage:
    source: MessageSource
    text: str
    attachments: list[dict] = field(default_factory=list)
    received_at: float = 0.0
    is_slash_command: bool = False
    slash_command_name: str | None = None

@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    raw: Any = None

@runtime_checkable
class PlatformAdapter(Protocol):
    name: str  # "slack"
    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
        ephemeral_user_id: str | None = None,
    ) -> SendResult: ...
    async def edit(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult: ...
    async def upload_file(
        self,
        chat_id: str,
        path: str,
        *,
        thread_id: str | None = None,
    ) -> SendResult: ...
```

That's the full interface. Three methods (`send`, `edit`, `upload_file`) for outbound + `connect`/`disconnect` for lifecycle. Inbound is push-based via a callback the gateway provides to the adapter on `connect()`.

### 4.3 Slack adapter (NEW)

`bridge/gateway/platforms/slack.py` — mirrors Hermes's pattern:

- Uses `slack-bolt` (`AsyncApp` + `AsyncSocketModeHandler`)
- Loads `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` from `~/.freyja/.env`
- On `connect()`: builds the app, registers event handlers for `message`, `app_mention`, slash commands; opens Socket Mode
- Multi-workspace: comma-split `SLACK_BOT_TOKEN` → multiple `AsyncWebClient` instances keyed by `team_id`
- Mention gating: bot in channels only responds if `@bot_user_id` appears in text; tracks "mentioned threads" to auto-respond to subsequent thread messages
- File downloads: on incoming files, downloads via `files.info` + `url_private` with bot token auth; caches to `~/.freyja/projects/<session_key>/incoming/`
- Streaming: gateway calls `adapter.send` for the first chunk, then `adapter.edit` for every subsequent batch of deltas

Estimated size: ~600-800 LOC (Hermes's slack.py is ~2500 LOC but a lot of that is edge case handling we can add iteratively).

### 4.4 Session bridging (lightweight modification to existing bridge)

A new module `bridge/gateway/session_router.py`:

```python
def session_key_for(source: MessageSource, *, threads_per_user: bool = False) -> str:
    """Mirror Hermes's session key scheme."""
    p = source.platform.value
    if source.chat_type == "dm":
        if source.thread_id:
            return f"freyja:{p}:{source.workspace_id}:dm:{source.chat_id}:{source.thread_id}"
        return f"freyja:{p}:{source.workspace_id}:dm:{source.chat_id}"

    parts = [f"freyja:{p}:{source.workspace_id}", source.chat_type, source.chat_id]
    if source.thread_id:
        parts.append(source.thread_id)

    isolate_user = True
    if source.thread_id and not threads_per_user:
        isolate_user = False
    if isolate_user and source.user_id:
        parts.append(source.user_id)

    return ":".join(parts)


async def route(message: IncomingMessage, bridge_state: _BridgeState) -> None:
    """Look up or create the Freyja session for this message, enqueue."""
    key = session_key_for(message.source)
    session = await bridge_state.ensure_session(
        session_id=key,
        coordination_strategy=_default_strategy_for(message.source),
        model_id=_default_model_for(message.source),
    )
    # Inject source context into the system prompt for this session
    session.gateway_source = message.source
    # Forward to existing message-queue machinery
    _schedule_or_queue_turn(session, message.text, message.attachments)
```

Existing `ensure_session` / `_schedule_or_queue_turn` already handle the "create if missing, queue if busy" semantics — we just call them with the structured key.

The session inherits everything Freyja: tool registry, sub-agent profiles, coordination strategy, memory, skills. The only Slack-specific thing the session knows is `gateway_source` — surfaced to the agent via the system prompt so it knows it's talking on Slack and can format accordingly.

### 4.5 Stream consumer for Slack (NEW)

`bridge/gateway/stream_consumer.py` — sits between the engine runner's stream events and the platform adapter:

```python
class SlackStreamConsumer:
    """Receives agent stream events, batches text deltas, emits Slack edits."""

    def __init__(self, adapter, source: MessageSource, edit_interval_ms: int = 500):
        self.adapter = adapter
        self.source = source
        self.edit_interval_ms = edit_interval_ms
        self._buffer = ""
        self._message_id: str | None = None
        self._last_emit = 0.0
        self._task = None

    async def on_stream(self, event):
        etype = getattr(event, "type", None)
        if etype == "text_delta":
            self._buffer += getattr(event, "text", "")
            await self._maybe_emit()
        elif etype == "tool_use_start":
            # Optional: emit "🔧 Calling read_file..." as a status message
            pass

    async def _maybe_emit(self):
        now = time.monotonic() * 1000
        if now - self._last_emit < self.edit_interval_ms:
            return
        await self._flush()

    async def _flush(self):
        if not self._buffer:
            return
        if self._message_id is None:
            result = await self.adapter.send(
                self.source.chat_id,
                self._buffer,
                thread_id=self.source.thread_id,
            )
            self._message_id = result.message_id
        else:
            await self.adapter.edit(
                self.source.chat_id,
                self._message_id,
                self._buffer,
            )
        self._last_emit = time.monotonic() * 1000

    async def finalize(self):
        await self._flush()
```

Hook into the existing runner via the `on_stream` callback. The runner already supports custom on_stream callbacks (added during the judge subagent work).

### 4.6 Daemon launcher + `freyja` CLI (NEW)

A new `bin/freyja` entry point (Python script, packaged via the existing bundled Python). Subcommands:

```
freyja setup           # interactive setup wizard, dispatches to subcommands
freyja setup slack     # just the Slack setup
freyja gateway start   # start the daemon (loads launchd plist)
freyja gateway stop    # stop the daemon
freyja gateway run     # foreground (dev / debug)
freyja gateway status  # show running state, connected platforms, recent activity
freyja gateway logs    # tail the daemon log
freyja slack manifest  # regenerate the Slack manifest (after Freyja update adds new slashes)
```

The CLI is thin — it loads + writes config files, manages launchd, and proxies to the gateway process. It does NOT spawn the gateway in-process; the gateway is always a separate launchd-managed process.

## 5. The `freyja setup slack` flow (concrete)

Step by step, with exact prompts and what gets written:

### Step 1 — Pre-flight

- Detect existing config (`~/.freyja/.env` already has `SLACK_BOT_TOKEN`)? Prompt to reconfigure vs keep.
- Detect launchd plist already installed? Note that we'll reload it after token change.
- Generate the Slack manifest JSON (deterministic from the current command registry).

### Step 2 — Generate + present manifest

Write `~/.freyja/slack-manifest.json` and copy contents to clipboard (via `pbcopy` on macOS). Show the user:

> Create your Slack app at https://api.slack.com/apps?new_app=1
>
> 1. Choose "From an app manifest"
> 2. Select your workspace
> 3. Paste the manifest (already in your clipboard, or read from `~/.freyja/slack-manifest.json`)
> 4. Press "Create"
>
> Press Enter when done.

### Step 3 — Socket Mode + app token

> In your new app, go to Settings → Socket Mode → toggle Enable to ON.
> Click "Generate an app-level token" — scope: `connections:write`. Name it whatever you like.
> Copy the token (starts with `xapp-`).
>
> Paste your App Token here: ▮

Validate format (`startswith("xapp-")`). Save to `~/.freyja/.env`.

### Step 4 — Install + bot token

> Go to Settings → Install App → Install to Workspace.
> Authorize the requested scopes.
> Copy the "Bot User OAuth Token" (starts with `xoxb-`).
>
> Paste your Bot Token here: ▮

Validate format. Save to `~/.freyja/.env`.

### Step 5 — Test connection

Run `auth_test()` against both tokens to verify they work and capture `team_id`, `bot_user_id`, workspace name. Show:

> ✓ Authenticated as @freyja in workspace Acme Corp (team T123456)
> ✓ Socket Mode token valid

If auth fails, surface Slack's error message verbatim and offer to re-enter tokens.

### Step 6 — Capability defaults

Show a brief explanation of what the Slack-routed agent can do by default, ask for confirmation:

> Default Slack agent permissions:
>   ✓ Read files in your workspace dir
>   ✓ Search the web
>   ✓ Spawn sub-agents
>   ✗ Write to files outside the project output dir
>   ✗ Run arbitrary shell commands
>   ✗ Use computer-use tools (mouse / keyboard / screen)
>   ✗ Use the browser tools
>
> You can change this anytime in ~/.freyja/gateway.yaml or via /perms in Slack.
>
> Accept defaults? [Y/n]

(This is a simpler version of the capability set design from `ALWAYS-ON-PLATFORM.md`. v1 = a flat default, not per-workflow capabilities. The mechanism is "tool exclude list applied to all Slack sessions.")

### Step 7 — Install gateway daemon

> Install Freyja gateway as a launchd service?
> (Runs in the background, starts at login, survives app quit) [Y/n]

If yes:
- Write `~/Library/LaunchAgents/co.freyja.gateway.plist` with `KeepAlive=true`, `RunAtLoad=true`, program args = `freyja gateway run`
- `launchctl load -w ~/Library/LaunchAgents/co.freyja.gateway.plist`
- Wait up to 10s for `~/.freyja/.gateway.pid` to appear
- Show ✓ Gateway started (pid <X>)

If no:
- Print the manual command: `freyja gateway run` (foreground)

### Step 8 — Final verification + walk-in

> Setup complete!
>
> Test it now:
>   1. Open Slack
>   2. Find @freyja in your DMs (might be under "Apps")
>   3. Type "hi"
>
> Useful commands:
>   freyja gateway status   — see what's running
>   freyja gateway logs     — tail the gateway log
>   freyja setup slack      — reconfigure
>
> In Slack:
>   /freyja help            — what the agent can do
>   /goal <objective>       — start a goal loop
>   /mode goal|kanban|bus   — change coordination strategy
>   /stop                   — interrupt the current turn

Done.

## 6. The Slack app manifest

Concrete YAML to ship (generated by `bridge/gateway/platforms/slack_manifest.py`, dynamic on the slash command list):

```yaml
display_information:
  name: Freyja
  description: Your Freyja agent on Slack
  background_color: "#0a0a0f"
features:
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  bot_user:
    display_name: Freyja
    always_online: true
  slash_commands:
    - command: /freyja
      description: Show what Freyja can do
    - command: /goal
      description: Start a goal loop with a judge
    - command: /mode
      description: Switch coordination strategy (bus / goal / kanban / isolated)
    - command: /model
      description: Switch the agent model
    - command: /stop
      description: Interrupt the current turn
    - command: /reset
      description: Start a fresh conversation
    - command: /queue
      description: Queue a message instead of interrupting
    - command: /status
      description: Show session info (model, mode, spend)
    - command: /perms
      description: Show and adjust the agent's tool permissions
  assistant_view:
    assistant_description: Chat with Freyja in DMs or @mention me in channels.
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - assistant:write
      - channels:history
      - channels:read
      - chat:write
      - commands
      - files:read
      - files:write
      - groups:history
      - groups:read
      - im:history
      - im:read
      - im:write
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - assistant_thread_started
      - assistant_thread_context_changed
      - message.channels
      - message.groups
      - message.im
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

Notes on the scope choices:
- `chat:write` is the minimum to send messages
- `*.history` is needed to read previous messages in a thread for context
- `files:read` + `files:write` for file attachments
- `commands` registers the slash commands
- `users:read` so the agent can fetch display names instead of `U123456`-style IDs
- We do NOT request `users:read.email` (privacy) or admin scopes

## 7. Schemas, file layout, IPC

### 7.1 File layout

```
~/.freyja/
├── .env                       # SLACK_BOT_TOKEN, SLACK_APP_TOKEN, etc.
├── .gateway.pid               # PID lock
├── gateway.yaml               # Gateway-level config (capability defaults, etc.)
├── slack-manifest.json        # Generated manifest (regenerable)
├── slack_tokens.json          # Optional: OAuth-discovered tokens per workspace
├── sessions/                  # Existing — per-session transcripts + sidecars
│   └── freyja:slack:T123:dm:U456/
│       ├── transcript.jsonl
│       ├── state.json
│       └── projects/
└── logs/
    └── gateway.log            # launchd-managed log
```

### 7.2 `gateway.yaml` (v1 shape — minimal)

```yaml
# Gateway-level config. Edited by setup wizard, hot-reloaded on change.

defaults:
  model: claude-opus-4-7
  reasoning_level: high
  coordination_strategy: bus      # 'bus' | 'goal' | 'kanban' | 'isolated'
  permission_tier: medium         # 'low' | 'medium' | 'high' (no 'yolo' over Slack)
  tool_exclude:
    - bash                         # no shell over Slack by default
    - computer
    - computer_use
    - click
    - move_mouse
    - type_text
    - press_key
    - screenshot
    - browser_execute_js
    - browser_screenshot

slack:
  allow_bots: none                 # 'none' | 'mentions' | 'all'
  allowed_user_ids: []             # if non-empty, deny everyone else (per workspace? per global?)
  mention_required_in_channels: true
  reply_in_thread: true
  reply_broadcast: false
```

### 7.3 launchd plist

`~/Library/LaunchAgents/co.freyja.gateway.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>co.freyja.gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/freyja</string>
    <string>gateway</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/USERNAME/.freyja/logs/gateway.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/USERNAME/.freyja/logs/gateway.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/Users/USERNAME</string>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
```

`KeepAlive.SuccessfulExit=false` means: restart unless the process exited cleanly (exit code 0). Matches Hermes's `Restart=on-failure` systemd semantics.

### 7.4 Capability set (v1, declarative)

The full capability model from `ALWAYS-ON-PLATFORM.md` is deferred. For Slack v1, we use the existing `tool_include` / `tool_exclude` mechanism on the session's tool registry. The `gateway.yaml` `defaults.tool_exclude` list is applied to every Slack session at session creation time. Operator can override per-session via `/perms` slash command.

The `/perms` command opens a Slack message with the current allowlist + denylist + a "request changes via the desktop app" link. Granular per-session perms editing happens in the renderer; over Slack you can only see and accept presets.

## 8. Showcase considerations

The user said "show her off to people." So the Slack experience needs to expose the genuinely-interesting Freyja capabilities, not just be a basic chat wrapper. Specifics:

- **Sub-agent work surfaces in the thread.** When the parent agent spawns sub-agents (explore-fast, code, verify, etc.), each sub-agent's completion posts a threaded reply: "🔍 explore-fast #1 found 3 sources" with collapsed findings. Operator can expand. This makes the "agent swarm" idea visible.
- **Goal mode in Slack.** `/goal write a blog post on X` arms the goal loop. The judge's verdict after each turn posts as a threaded reply with the criteria status. The streaming response edits in place as the agent works. This is a killer demo — the agent visibly iterating with self-judgment.
- **Memory works transparently.** Operator says "remember that I'm vegetarian." Memory fires. Future messages reference it. Memory entries are visible via `/memory` slash command (returns a Slack-formatted list).
- **Skills load on relevant cues.** Operator says "help me set up a touchdesigner network." The `touchdesigner-mcp` skill auto-loads. Visible via "📚 loaded skill: touchdesigner-mcp" status message in the thread.
- **Image inputs work.** Operator drops a screenshot of an error message. Agent reads it via the existing image attachment path.
- **The desktop app mirrors Slack sessions.** Every Slack conversation shows up as a session in the Freyja sidebar with `[slack]` badge. Operator can drill in for the full transcript, tool calls, branch the conversation, etc. This is unique to Freyja — Hermes doesn't have a rich renderer to mirror to.
- **Slash command for "show me what you can do."** `/freyja` posts a beautifully-formatted block-kit message with feature highlights and example commands. The literal demo card.

What we deliberately don't expose over Slack in v1:
- Computer-use (mouse/keyboard/screen) — too easy to misuse unattended.
- Bash by default — escalation risk.
- Browser tools — needs UI to be useful.
- Kanban autopilot — works fine in Slack but the visual board is the point.

These can be granted per-session via the desktop app when the operator wants them.

## 9. Phased plan

### Phase A — Daemon entry point + gateway scaffolding (~3-5 days)

- Add `bin/freyja` entry script + `pyproject.toml` script entry
- Add `bridge/gateway/__init__.py`, `run.py` with `main()` that starts the bridge state in standalone mode
- Add PID lock at `~/.freyja/.gateway.pid`
- Add launchd plist generator + installer + `freyja gateway start/stop/status/run/logs` subcommands
- Test: `freyja gateway run` foreground works, agent state survives a separate terminal session
- **Deliverable**: bridge runs as a daemon, can be installed/started/stopped via CLI

### Phase B — Platform adapter Protocol + session router (~2-3 days)

- `bridge/gateway/platforms/base.py` with the `PlatformAdapter` Protocol + dataclasses
- `bridge/gateway/session_router.py` with `session_key_for()` + `route()` functions
- A no-op "stdin" adapter so the existing renderer→bridge flow keeps working through the same path
- Wire `_BridgeState` to accept push-based messages from adapters in addition to stdin commands
- **Deliverable**: gateway can route messages through multiple input sources

### Phase C — Slack adapter (~4-6 days)

- `bridge/gateway/platforms/slack.py` — full Socket Mode adapter
- Add `slack-bolt` to `pyproject.toml` + `requirements.txt`
- Implement `connect`, `disconnect`, `send`, `edit`, `upload_file`
- Implement message handler with mention gating, thread tracking, deduplication
- Implement slash command handler with ephemeral response routing
- Implement file download for incoming attachments
- Implement streaming response via `SlackStreamConsumer` (progressive edits)
- Multi-workspace token support
- **Deliverable**: Slack adapter works end-to-end with hand-pasted tokens

### Phase D — `freyja setup slack` wizard (~3-4 days)

- `bridge/gateway/setup/slack.py` with the interactive wizard
- Manifest generator in `bridge/gateway/platforms/slack_manifest.py` (dynamic on slash command list)
- Token validation via Slack `auth.test`
- `.env` writer (preserves comments, atomic update)
- launchd plist install path
- Health check + first-message walkthrough
- **Deliverable**: zero-to-chatting in under 5 minutes

### Phase E — Slack-specific session context + capabilities (~2-3 days)

- Inject `gateway_source` into session system prompts (so agent knows it's on Slack, who the user is, what channel)
- Apply `tool_exclude` from `gateway.yaml` to Slack sessions
- Implement `/perms`, `/mode`, `/status`, `/freyja` slash commands
- Per-session strategy override via `/mode`
- **Deliverable**: Slack sessions feel different from CLI sessions in the right ways

### Phase F — Renderer mirror (~2-3 days)

- Each Slack session shows up in the desktop app's sidebar with a `[slack]` badge
- Operator can drill in to see the full transcript exactly as in any other Freyja session
- Renderer doesn't need to know it's a Slack session — just renders the transcript like any other
- Notification surfaces: optional macOS notification when a Slack message arrives that needs operator attention
- **Deliverable**: Slack sessions are first-class Freyja sessions in the UI

### Phase G — Polish + showcase touches (~3-4 days)

- `/goal` integration: judge verdicts post as threaded replies; sub-agent completions threaded
- `/freyja` info card with Block Kit formatting
- Slash command help: `/freyja help <command>` deep-links to docs
- Streaming UX polish: status indicators when calling tools ("🔍 searching..." while a subagent runs)
- Error messages over Slack: budget exceeded, capability denied, tool failed — all routed gracefully

**Total realistic timeline**: ~3 weeks of focused work for Phases A–E (the "you can use it" milestone). Phase F and G are ongoing polish.

## 10. Open questions

Lock in before code:

1. **CLI entry point**. Ship `freyja` (preferred) or `freyja-cli` (less risk of collision)? Where does the binary live — `/usr/local/bin/freyja` (admin install) or `~/.local/bin/freyja` (no-sudo install)?
2. **Wizard surface**. Pure CLI (like Hermes) or in-app from a settings screen? I'd argue CLI: launchd installation requires the user to be in a terminal anyway, and the wizard maps naturally to interactive prompts.
3. **Multi-workspace activation**. Comma-separated tokens in `.env` (Hermes style) or one-workspace-per-line in `slack_tokens.json`? Tradeoff: `.env` is simpler but ugly when 5 workspaces; `slack_tokens.json` is structured but harder to edit by hand. I lean structured.
4. **Default coordination strategy for Slack sessions**. `bus`? `isolated`? Something new like "interactive"? Probably `bus` to match the desktop default, but Slack DMs feel more like single-session chat — `isolated` might be better. Worth UX testing.
5. **What's the desktop app's relationship to the daemon?** When the daemon is installed: does the desktop app launch its own bridge subprocess (today's behavior) or connect to the daemon? v1 simplest: keep desktop app as-is, daemon is separate. v2: unify so daemon is the only bridge process.
6. **Per-workspace allowlist semantics**. `SLACK_ALLOWED_USERS=U1,U2` — does this apply per-workspace or globally? Globally is simpler but a user_id in workspace A is meaningless in workspace B. Probably per-workspace mapping: `SLACK_ALLOWED_USERS=T123:U1,U2;T456:U3`.
7. **Should Slack DMs share state with desktop sessions for the same user?** E.g. operator asks Freyja something on Slack, then opens the desktop app — should the Slack session show up there? My answer: yes, every session shows up in the desktop sidebar regardless of source. But the session is otherwise independent — desktop conversations don't bleed into Slack and vice versa.
8. **Capability v1 surface**. Just `tool_exclude` defaults in `gateway.yaml` + a few presets? Or full capability sets per workflow as in `ALWAYS-ON-PLATFORM.md`? I'd say defer the full capability model — v1 is "deny dangerous tools by default, operator can grant via desktop app."
9. **What does `/stop` do mid-stream?** Cancel the current LLM call cleanly (returning whatever's been generated) or hard-abort? Probably soft: cancel the LLM but finalize the in-progress message.
10. **Cost visibility over Slack**. Should `/status` show $X spent today? Should the agent show a small footer like "12k tokens · $0.04"? Risk: operator-facing cost numbers in shared channels = social weirdness. Probably DM-only; suppress in shared channels.

## 11. Risks

- **`slack-bolt` library version drift.** It's the canonical Slack Python library but has had breaking changes. Pin to a known-good version and validate on every upgrade.
- **Socket Mode connection stability.** WebSocket connections behind corporate proxies can be flaky. Need solid exponential backoff + reconnect (slack-bolt handles this, but we should log + surface in `/status`).
- **Token theft.** `.env` plaintext = anyone with file access can use the bot. Mitigation: set file mode 0600 on `.env`, document the risk in the setup wizard, defer keychain storage to v2.
- **Bot rate limits.** Slack imposes 1 message/second per channel for posts. Streaming via `chat.update` is more permissive but not unlimited. Need to throttle the `edit_interval_ms` and respect 429s.
- **Multi-workspace identity confusion.** If operator installs bot in 3 workspaces, the bot has 3 different `bot_user_id`s. Session keying handles this via `team_id` in the key, but the user-facing experience needs to be clear about which workspace they're in.
- **Demo failure modes.** When demoing, the bot says something weird or refuses to do something. Standard agent risk. Mitigation: pre-flight the demo (have known-good prompts that show off the features) and have the desktop app handy to debug live.
- **Daemon + desktop bridge conflict.** Initial v1 has two Python bridge processes (one daemon, one Electron-spawned). They share `~/.freyja/sessions/` and could race on file writes. Mitigation: only the daemon writes session state; the Electron bridge only reads + emits events. Or: a clean cut where only the daemon exists. Probably worth doing the clean cut as part of phase A.
- **First-time setup confusion.** Slack's app creation UI changes; the manifest path might not work the same way 6 months from now. Mitigation: link to Slack's docs in the wizard, screenshot the exact UI steps in `docs/SLACK-SETUP-WALKTHROUGH.md`.

## 12. What success looks like

Concrete acceptance criteria:

- I run `freyja setup slack` in a terminal and 5 minutes later I'm in Slack DMing the bot and getting responses.
- I close the Freyja desktop app. The Slack bot keeps responding.
- I reboot my Mac. The Slack bot is back online within 30 seconds of login.
- I @mention the bot in a channel. It responds in-thread. Subsequent messages in that thread don't need re-mention.
- I drop a screenshot into a DM. The bot reads it via the existing image tools and responds about its content.
- I type `/goal write me a quick async http client with retry`. The bot kicks off a goal loop. The judge's verdict after each turn posts as a threaded message. The bot's response streams in place as it's generated.
- I open the Freyja desktop app. The Slack conversation shows up in the sidebar with a `[slack]` badge. I drill in and see the full transcript with tool calls.
- I install Freyja's Slack app in a second workspace. Both workspaces work concurrently. Conversations don't mix.
- I run `freyja gateway status`. It shows the connected Slack workspaces, active sessions, recent activity.
- I run `freyja gateway logs`. I get a tail of structured logs I can debug from.
- I show this off to a friend. They get it within 60 seconds of seeing it.

That last criterion is the real one. If the demo doesn't make people lean in, we haven't done it well enough.

## 13. Document changelog

- 2026-05-27: Initial draft.
