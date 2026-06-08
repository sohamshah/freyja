<p align="center">
  <img src="assets/icon.png" width="108" alt="Freyja icon" />
</p>

<h1 align="center">Freyja</h1>

<p align="center">
  <strong>A Mac-native cockpit for long-running, visual, multi-agent work.</strong>
</p>

<p align="center">
  Freyja is an agentic desktop app that can write code, browse the web, operate your Mac,
  spawn specialist subagents, preserve the full session trajectory, and show what is
  happening while the work is still alive.
</p>

<p align="center">
  <img src="docs/assets/freyja-system-map.svg" alt="Freyja system map" />
</p>

> **Platform:** macOS on Apple Silicon  
> **Status:** internal alpha, `v0.1.0`  
> **Stack:** Electron · React · TypeScript · Python · Rust · pyo3

---

## What It Is

Most agent apps are either chat boxes with tools bolted on, or opaque runners that become impossible to inspect once the work gets large. Freyja is built for the messy middle: multi-hour sessions, many subagents, computer-use screenshots, tool traces, files changing under your feet, context compaction, and models with different strengths working in parallel.

The product goal is simple: give powerful agents a real desktop mission-control surface without hiding the machinery. You should be able to watch the swarm, inspect the evidence, jump to a file edit, see when context was compacted, and recover the trajectory later.

---

## Setup

### Prerequisites

- macOS on Apple Silicon
- Node 18+, Python 3.11+, [`uv`](https://docs.astral.sh/uv/), Rust toolchain

```bash
brew install node python uv rustup-init && rustup-init
```

### Install

```bash
git clone https://github.com/sohamshah/freyja.git && cd freyja
cp .env.example .env   # fill in your API keys — see the .env section below
npm install
uv sync --extra dev

# One-time native extension build (macOS screen capture / input / accessibility)
cd native/freyja_native && uv run maturin develop --release && cd ../..
```

### Run

**Development (hot reload):**

```bash
npm run dev
```

Vite hot-reloads the renderer. Bridge and engine edits require an app restart. Computer-use permissions are gated by the parent process, so for realistic screen/input testing use the packaged build instead.

**Packaged `.app` build (needed for computer-use, TCC permissions):**

```bash
npm run setup-signing-cert   # one-time — creates a self-signed Keychain cert so
                             # TCC grants persist across rebuilds
npm run rebuild              # builds, signs, installs to /Applications, launches
```

`npm run rebuild` replaces `/Applications/Freyja.app` and preserves macOS TCC grants (Screen Recording, Accessibility, Input Monitoring, Full Disk Access) across rebuilds by signing every build with the same self-signed cert. You grant permissions once.

<details>
<summary>Manual cert setup (if you prefer the Keychain GUI)</summary>

1. Open **Keychain Access → Certificate Assistant → Create a Certificate…**
2. Name it `Freyja Dev`, Identity Type `Self Signed Root`, Certificate Type `Code Signing`. Tick **Let me override defaults** to extend validity past one year.
3. Right-click the cert → **Get Info → Trust → Code Signing: Always Trust**.

Verify with:
```bash
security find-identity -v -p codesigning | grep "Freyja Dev"
```
</details>

Other build targets:

```bash
npm run package   # build .app into out/mac-arm64/ (no install)
npm run dist      # build .app + create a DMG
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in what you need.

### Model providers — set at least one

| Variable | Provider | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic | Claude Opus / Sonnet / Haiku. Default model is `claude-sonnet-4-6`. |
| `OPENAI_API_KEY` | OpenAI | GPT-5.x family |
| `CEREBRAS_API_KEY` | Cerebras / Z.ai | `zai-glm-4.7` |
| `FIREWORKS_API_KEY` | Fireworks | Kimi K2, DeepSeek V4 Pro, MiniMax M2, GLM 5, Qwen 3.6 |
| `GEMINI_API_KEY` | Google | Gemini 2.5 / 3.x; also required for the `analyze_video` tool |

### Tools

| Variable | Used by | Notes |
|---|---|---|
| `PARALLEL_API_KEY` | `web_search`, `web_fetch`, `web_research` | Required for all web/search tools |
| `QUIVER_API_KEY` | `generate_svg` | Quiver AI Arrow renderer for high-fidelity SVG generation |

### Slack gateway

| Variable | Notes |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-…` Bot token from **Settings → Install App**. Comma-separate for multi-workspace. |
| `SLACK_APP_TOKEN` | `xapp-…` App-Level Token (scope: `connections:write`) from **Settings → Socket Mode**. |

See [Slack gateway setup](#slack-gateway) below for the guided wizard.

### Freyja runtime (optional)

| Variable | Default | Notes |
|---|---|---|
| `FREYJA_MODEL` | `claude-sonnet-4-6` | Default model for new sessions |
| `FREYJA_WORKSPACE` | `~` | Root workspace directory |
| `FREYJA_PERMISSION_AUTO` | `low` | Auto-approval tier: `low` (read-only auto), `medium`, `high`, `yolo` |
| `FREYJA_DEBUG_LOG` | `0` | Set to `1` for verbose bridge logging |
| `FREYJA_COMPUTER_ENABLED` | `0` | Set to `1` to enable computer-use tools |
| `FREYJA_IMAGE_MODEL` | _(provider default)_ | Model used for `generate_image` calls |
| `FREYJA_FRAME_DUMP` | `0` | Set to `1` to dump computer-use frames to disk |
| `FREYJA_FRAME_DUMP_DIR` | _(temp dir)_ | Directory for frame dumps |
| `FREYJA_HEADLESS` | `0` | Run bridge without Electron UI |
| `FREYJA_PERMISSION_TIMEOUT_SEC` | `30` | Seconds to wait for permission approvals |

**Minimum viable `.env` to get started:**

```bash
ANTHROPIC_API_KEY=sk-ant-...
PARALLEL_API_KEY=...
```

---

## Slack Gateway

Freyja includes a background gateway daemon that lets you chat with the same agent engine from Slack, without the desktop app running. It uses Socket Mode — no public URL or inbound firewall rules required.

### Guided setup

```bash
freyja setup slack
```

The wizard walks you through:
1. Generating the Slack app manifest and copying it to your clipboard
2. Creating the app at api.slack.com
3. Enabling Socket Mode and collecting the App-Level Token (`xapp-…`)
4. Installing to your workspace and collecting the Bot Token (`xoxb-…`)
5. Verifying tokens via `auth_test`
6. Installing and starting the gateway as a launchd service

### Manual gateway management

```bash
freyja gateway run          # foreground daemon (dev / debug)
freyja gateway install      # install launchd plist + start
freyja gateway uninstall    # stop + remove plist
freyja gateway start        # launchctl start (already-installed plist)
freyja gateway stop         # graceful SIGTERM
freyja gateway status       # show running pid + connected platforms
freyja gateway logs         # tail the gateway log
freyja gateway logs --follow
```

### Gateway config (`~/.freyja/gateway.yaml`)

```yaml
defaults:
  model: claude-sonnet-4-6        # default model for gateway sessions
  coordination_strategy: bus      # bus | isolated | kanban

slack:
  # Map of workspace_id → list of allowed user IDs.
  # Empty list = allow any user in that workspace.
  # Workspace absent = deny-all (safe default for shared workspaces).
  allowed_user_ids:
    T012345: [U001, U002]
  enforce_workspace_allowlist: true
  mention_required_in_channels: true  # DMs always respond; channels need @mention
  reply_in_thread: true
  enable_tool_filter: false           # set true in shared workspaces to restrict tools
```

---

## What It Can Do

### Desktop chat

Streaming text, tool calls and results, inline computer-use frames, pasted images, file diffs, and a persistent session sidebar. Supports multiple panes for side-by-side session views.

### Computer use

Drive macOS directly: screenshot, click, type, scroll, move mouse, inspect windows, read accessibility trees, find elements, query displays, and execute multi-step UI automation loops. Requires Screen Recording, Accessibility, and Input Monitoring permissions — granted once when using the signed `.app` bundle.

### Subagent swarms

Spawn background subagents with dedicated profiles for different roles. The active coordination strategy controls how they collaborate:

| Strategy | Behavior |
|---|---|
| `bus` | Agents publish and read shared findings on a message bus. Siblings build on each other's discoveries. |
| `isolated` | Agents are fully independent leaf workers. The parent synthesizes. |
| `kanban` | A session board with cards, dependencies, status lanes, and structured handoffs. |

Swarms are visualized as a round-based mission graph: spawn waves, publish/read edges, kanban board state, and bus traffic stay visible after the swarm completes.

### Model mesh

29 model profiles across 5 provider families:

| Family | Models |
|---|---|
| Anthropic | Claude Opus 4.8, Opus 4.7, Sonnet 4.6, Opus 4.6, Sonnet 4.5, Opus 4.5, Haiku 4.5 |
| OpenAI | GPT-5.5, GPT-5.4, GPT-5.4 Pro, GPT-5.4 Mini, GPT-5.4 Nano, GPT-5.3 Codex |
| Cerebras | Z.ai GLM 4.7 |
| Fireworks | Kimi K2.6, Kimi K2.5, DeepSeek V4 Pro, GLM 5.1, GLM 5, MiniMax M2.7, MiniMax M2.5, Qwen 3.6 Plus |
| Google | Gemini 3.1 Pro Preview, Gemini 3.5 Flash, Gemini 3.1 Flash, Gemini 3.1 Flash Lite, Gemini 2.5 Pro, Gemini 2.5 Flash |

The model picker tracks context window, thinking-mode support, API key availability, and per-model reasoning history behavior. Provider adapters live in `engine/*_provider.py`; the catalog is in `engine/providers.py`.

### Generative UI widgets

Agents can render interactive HTML/SVG widgets directly in the conversation — metric dashboards, diagrams, charts, and structured input forms that submit back as the next user message.

- `widget_spec` returns the design-system reference (CSS classes, color ramps, Tabler icons, elicit form chrome, hard constraints)
- `show_widget` emits a `widget_render` event carrying the agent's HTML/SVG fragment, sandboxed in an iframe with a Freyja design-system runtime

### Context and compaction

Long sessions are first-class. Freyja tracks context pressure, compaction events, image history, and request media policy:

- Token pressure triggers pruning and LLM-based compaction automatically
- Compactions are visible in the transcript with before/after token estimates and a summary
- Computer-use tool-result images are pruned from provider requests after the most recent few frames, while the UI retains the visual trail
- The mission dashboard shows compaction cards and image-history policy

### Memory and skills

- Durable memory is file-backed under `~/.freyja/knowledge` and project-aware
- Skills are markdown files discovered from `~/.freyja/skills`, `~/.claude/skills`, `knowledge/`, and `.freyja/skills`
- Skill loading is explicit, visible in the UI, and tracked with usage metadata
- Custom agent profiles can be added as markdown files under `.freyja/agents` or `~/.freyja/agents`

### Goal mode

`/goal <objective>` sets a judge-evaluated goal loop. A skeptical judge agent periodically assesses whether the stated goal has been satisfied and can stop the loop or escalate blockers to the operator.

### Scheduled jobs

`schedule` tool lets agents create, inspect, and manage cron-style jobs that fire agent turns at specified times. Managed via `/schedule` in the chat or the `freyja gateway` CLI.

---

## Tool Reference

The full tool surface, organized by category:

| Category | Tools |
|---|---|
| **File system** | `read_file`, `write_file`, `edit_file`, `edit_json`, `glob`, `grep`, `list_directory`, `artifacts` |
| **Shell** | `bash` |
| **Web / search** | `web_search`, `web_fetch`, `web_research` |
| **Browser** | `browser_execute_js`, `browser_screenshot` |
| **Computer use** | `screenshot`, `click`, `type_text`, `press_key`, `key_down`, `key_up`, `scroll`, `move_mouse`, `cursor_position`, `focus_window`, `list_windows`, `list_displays`, `inspect_region`, `find_element`, `read_ax_tree`, `wait`, `computer_use` |
| **Creative media** | `generate_image`, `generate_svg`, `analyze_video`, `view_image`, `send_attachment` |
| **Generative UI** | `widget_spec`, `show_widget` |
| **Memory / knowledge** | `memory`, `session_memory`, `working_memory`, `recall`, `record_user_preference` |
| **Skills** | `list_skills`, `search_skills`, `load_skill`, `propose_skill` |
| **Subagents** | `sub_agent`, `subagents`, `talk`, `list_agent_sessions` |
| **Coordination** | `tasks`, `kanban`, `publish_finding`, `read_findings` |
| **Context** | `summarize_context` |
| **Goals / scheduling** | `schedule` |
| **Utilities** | `tool_search` |

### Built-in subagent profiles

| Profile | Purpose |
|---|---|
| `general` | Default delegation; inherits parent model and safe tools |
| `explore` | Deep web/file/codebase research with bus publishing |
| `explore-fast` | Fast fanout lookup over a rotating low-latency model set |
| `code` | Isolated file/code edits with high thinking |
| `verify` | Independent read-only validation after implementation |
| `plan` | Read-only implementation planning |
| `review` | Read-only code review focused on bugs and regressions |
| `test` | Build/test execution and failure diagnosis |
| `browser-qa` | Frontend behavior, layout, and screenshot checks |
| `performance` | Profiling and low-risk optimization investigation |
| `docs` | Documentation and design-document writing |
| `memory-curator` | Skill and memory hygiene |
| `judge-deep` | Skeptical third-party adjudication for goal-mode loops |
| `skill-drafter` | Reviews conversations and proposes skill candidates for approval |
| `specifier` | Kanban card specifier — expands triage cards into structured specs |

Custom profiles can be added as markdown files under `.freyja/agents/` or `~/.freyja/agents/`.

---

## Architecture

Freyja ships as a single `.app` bundle with four layers:

| Layer | Tech | What it owns |
|---|---|---|
| UI | Electron, React, Vite, Tailwind, Zustand | Mission UI, chat, dashboard, diffs, artifacts, local persistence |
| Bridge | Python asyncio over JSONL stdin/stdout | Sessions, commands, subagent orchestration, skills, memory, events |
| Engine | Pure Python | Agent loop, provider adapters, compaction, context pressure, tool dispatch |
| Native | Rust + pyo3 | macOS screen capture, input, windows, accessibility primitives |

At runtime Electron spawns `bridge/freyja_bridge.py`, talks to it over JSONL, and proxies capture/input through the main process so macOS TCC permissions are owned by the app bundle instead of a random shell process.

```
Electron + React UI  →  Python JSONL bridge  →  Async agent engine
                                                         ↓
                                                 Provider adapters
                                               (Claude / GPT / Gemini / ...)
                                                         ↓
                                                   Tool registry
                                                         ↓
                                              Rust native macOS layer
```

The bridge also hosts the gateway daemon (`bridge/gateway/`) as a separate process tree — the Slack gateway runs independently of the Electron app.

### Project layout

```
freyja/
├── src/
│   ├── main/           Electron main process and native proxies
│   ├── preload/        contextBridge API
│   ├── renderer/       React UI, mission dashboard, activity rail
│   └── shared/         IPC and event types
├── bridge/
│   ├── freyja_bridge.py      JSONL bridge, sessions, commands, events
│   ├── gateway/              Slack gateway daemon + CLI + setup wizard
│   ├── knowledge/            File-backed memory and skill stores
│   └── tools/                Desktop tool registry and implementations
├── engine/
│   ├── runner.py             Async agent loop
│   ├── session.py            Transcript, compaction, image pruning
│   ├── compaction.py         LLM summary compaction strategy
│   ├── providers.py          Model registry and provider mesh
│   └── *_provider.py         Per-provider adapters
├── native/freyja_native/     Rust pyo3 macOS capture/input/window/AX layer
├── docs/                     Architecture, performance, skills, research
├── scripts/                  Dev, packaging, signing, trajectory export
├── tests/                    Python regression tests
└── .freyja/skills/           Starter project skills
```

---

## Useful Commands

```bash
# Development
npm run build
npm run dev

# Testing
uv run --extra dev pytest -q
python3 -m py_compile bridge/freyja_bridge.py engine/runner.py

# Packaging
npm run rebuild              # build + sign + install + launch
npm run package              # build .app only
npm run dist                 # build .app + DMG

# Bridge standalone (debugging without Electron)
uv run python -m bridge.freyja_bridge
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| App starts in demo mode | Check `.env` — at minimum `ANTHROPIC_API_KEY` must be set. Check bridge startup logs (`FREYJA_DEBUG_LOG=1`). |
| Packaged bridge can't import modules | Re-run `./scripts/bundle-python.sh` then `npm run rebuild` |
| `freyja_native` missing in dev | Run `uv run maturin develop --release` inside `native/freyja_native/` |
| Computer-use permissions fail | Grant Screen Recording, Accessibility, Input Monitoring (and Full Disk Access for paths outside `~/`) to `/Applications/Freyja.app`, then restart. If grants vanish after each rebuild, run `npm run setup-signing-cert` — `npm run rebuild` needs the `Freyja Dev` self-signed identity for TCC to persist. Reset with `tccutil reset All co.freyja.desktop`. |
| `web_search` / `web_fetch` fail | Set `PARALLEL_API_KEY` in `.env` |
| `generate_svg` returns auth error | Set `QUIVER_API_KEY` in `.env`. Check your Quiver API credits at quiverlabs.ai. |
| Context grows large with screenshots | Use the dashboard image-policy view; provider requests auto-prune old tool-result images after the most recent few frames |
| Slack gateway not responding | Run `freyja gateway status` and `freyja gateway logs`. Verify `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set in `~/.freyja/.env`. |
| Adding a new model | See `docs/ADDING-A-MODEL.md` — there are 13+ codepoints that need matching updates |

---

## Documentation

| Doc | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | System architecture deep dive |
| `docs/ADDING-A-MODEL.md` | Checklist for adding a new model to the mesh |
| `docs/PERFORMANCE-DEEP-DIVE.md` | Renderer, media, session, and subagent performance analysis |
| `docs/SKILLS-MEMORY-DESIGN.md` | Skills and memory design |
| `docs/COMPACTION-DECISION-DRAFT.md` | Context compaction strategy and cooperative protocol |
| `docs/SLACK-GATEWAY.md` | Slack gateway protocol, manifest, and scopes reference |
| `docs/TRAJECTORY-TRAINING.md` | Session trajectory export formats |
| `docs/WARP-FILE-EDIT-UX-RESEARCH.md` | File-edit UX research and design notes |

---

## License

MIT.
