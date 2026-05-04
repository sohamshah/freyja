# Freyja

**Agentic AI desktop app for macOS.** A standalone Electron + React UI on top
of a Python agent engine, with native computer-use capabilities, specialized
subagents, a session message bus, and full trajectory persistence.

> **Platform:** macOS (Apple Silicon). Linux/Windows possible later.
> **Status:** Internal alpha — `v0.1.0`.

---

## What's in the box

A single `.app` bundle containing four layers:

| Layer | Tech | Lives in |
|-------|------|----------|
| **UI** | Electron + React + Vite + Tailwind + Zustand | `src/main`, `src/preload`, `src/renderer`, `src/shared` |
| **Bridge** | Python (`asyncio`, JSONL over stdin/stdout) | `bridge/` |
| **Engine** | Pure Python agent loop (provider chain, compaction, tool dispatch) | `engine/` |
| **Native** | Rust + `pyo3` (CoreGraphics, Accessibility, Enigo) | `native/freyja_native/` |

The Electron main process spawns `bridge/freyja_bridge.py` as a child, talks
to it over JSONL, and proxies screen-capture / input to the bundled Python
runtime so TCC permissions inherit cleanly.

---

## Feature snapshot

- **13 model providers** out of the box: Claude Opus/Sonnet/Haiku 4.6 + 4.5,
  GPT-5.4 family, GLM 4.7 (Cerebras), Kimi K2.5, GLM 5, MiniMax M2.5
  (Fireworks). 1M-token context on Sonnet 4.6, max thinking on Opus 4.6.
- **5 specialized agent types**: `general`, `explore`, `explore-fast`,
  `code`, `verify`. Each defines its own model, thinking effort, tool
  whitelist, and system prompt — see `bridge/tools/agent_types.py`.
- **30+ tools** wired in: file read/write/edit, bash, web search/fetch,
  glob/grep, computer-use (screenshot, click, type, scroll, AX tree, find
  element), 14 native macOS automation primitives, sub_agent orchestration,
  message bus, memory, tool search.
- **Session message bus** — siblings publish findings to a shared
  append-only log; live activity rail in the swarm monitor.
- **Artifact persistence** — every subagent writes its full output to
  `~/.freyja/sessions/{id}/artifacts/{sub_id}.md`, surviving truncation
  and compaction. In-app preview for markdown, JSON, CSV, SVG, code,
  HTML, and images.
- **Transcript persistence** — full engine transcripts saved per turn so
  closing and reopening the app restores context. Legacy fallback for
  pre-persistence sessions extracts a UI-message summary.
- **Trajectory export** in v3 JSON, ATIF v1.6, and ShareGPT formats —
  see `docs/TRAJECTORY-TRAINING.md` and `scripts/convert-to-atif.py`.
- **Computer use** with full TCC inheritance (Screen Recording,
  Accessibility) — capture proxy + input proxy in the Electron main
  process delegate to the bundled Python.

---

## Quick start (development)

> Use this for day-to-day development. Hot-reloads renderer changes via
> Vite. Uses your local `.venv`, not a bundled Python.

```bash
# 1. Install prerequisites
#    Node 18+, Python 3.11+, uv (https://docs.astral.sh/uv/),
#    and the Rust toolchain (rustup) — only needed once for the
#    native extension.
brew install node python uv rustup-init && rustup-init

# 2. Configure API keys
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY.

# 3. Install dependencies
npm install
uv sync

# 4. Build the native macOS extension (one time)
cd native/freyja_native
uv run maturin develop --release
cd ../..

# 5. Run
./launch.sh
# or directly:
npm run dev
```

The dev orchestrator (`scripts/dev.mjs`) starts:

- `vite dev` for the renderer (HMR on `http://localhost:5173`)
- `esbuild --watch` for `main.cjs` and `preload.cjs`
- `electron .` once Vite is ready, pointed at the dev URL

Edit any TypeScript/React file — the renderer hot-reloads. Edit any
Python file in `bridge/` or `engine/` — restart the bridge with
`⌘R` (DevTools reload) or pick **Restart Bridge** from the debug drawer.

### Running the bridge standalone (no UI)

Useful for debugging agent loops without Electron in the way:

```bash
uv run python -m bridge.freyja_bridge
# Writes JSONL events to stdout, reads commands from stdin.
# Send a test message:
echo '{"type":"send_message","content":"hello","sessionId":"test"}' \
  | uv run python -m bridge.freyja_bridge
```

---

## Building a shippable `.app` (production)

This is what you ship to a tester or release. Produces a self-contained
`.app` with bundled Python runtime — the user does **not** need Python,
Node, or Rust installed.

### One-time setup

You need the same prerequisites as development plus a built native
extension and a synced venv:

```bash
npm install
uv sync
cd native/freyja_native && uv run maturin develop --release && cd ../..
```

### Step 1 — Bundle Python

This packs the project's `.venv` interpreter, stdlib, and all
site-packages (including the `freyja_native` Rust extension) into
`python-bundle/`. The output is ~85 MB.

```bash
./scripts/bundle-python.sh
```

The script:

1. Copies `python3` from the venv's base interpreter
2. Copies the full stdlib for that Python version
3. Copies `site-packages` from the venv (your installed deps + the
   compiled `_native.abi3.so`)
4. Strips test files, docs, and `__pycache__` to shrink the bundle
5. Re-signs every `.so` and dylib with ad-hoc codesigning so they pass
   macOS Gatekeeper and inherit TCC permissions

You only need to re-run this when:
- Python deps change (after `uv add`/`uv sync`)
- The native extension is rebuilt (after editing Rust code)
- You bump the Python version

### Step 2 — Build & package

```bash
npm run package
```

This:

1. `vite build` → `dist-renderer/` (renderer JS/CSS, ~300 KB gzipped)
2. `esbuild` → `dist-main/main.cjs` and `dist-preload/preload.cjs`
3. `electron-builder --mac --dir` → `out/mac-arm64/Freyja.app/`
4. `scripts/sign-resources.js` (afterPack hook) — ad-hoc signs every
   binary inside the `.app`, in inside-out order, so the outer signature
   seal is consistent

The result lives at `out/mac-arm64/Freyja.app`. You can launch it
directly:

```bash
open out/mac-arm64/Freyja.app
```

### Step 3 — Build a DMG (optional, for distribution)

```bash
npm run dist
```

This runs `npm run package` then `scripts/create-dmg.mjs`, which uses
`hdiutil` directly to produce `out/Freyja-0.1.0.dmg`. We bypass
electron-builder's built-in DMG step because it fails on arm64 macOS
Tahoe (APFS → UDZO conversion errors) — `hdiutil create -srcfolder`
works on every macOS version.

The DMG is what you send to a tester. They drag it to `/Applications`
and double-click. No `.dmg` signing — it's ad-hoc, so first launch
requires right-click → Open to bypass Gatekeeper.

---

## Code signing & notarization

Currently we ship **ad-hoc signed** (no Developer ID, no notarization).
This is fine for internal alpha but means:

- Gatekeeper warns on first launch
- Computer-use TCC grants are scoped to the bundle ID, so users have
  to re-grant after every rebuild that changes the signature
- Some macOS versions may quarantine the `.dmg` — use `xattr -cr` to clear

For a real release, set up a Developer ID certificate, drop the
`identity: null` from `package.json`'s `build.mac` block, and add a
notarization step after `electron-builder`.

---

## What runs at runtime

When you launch the `.app`:

1. `dist-main/main.cjs` boots the Electron main process
2. Main process spawns `bridge/freyja_bridge.py` using
   `Resources/python-bundle/bin/python3`
3. Bridge reads `Resources/.env` for API keys and announces capabilities
4. Renderer connects via the `harness:bridge-event` IPC channel
5. User sends a message → main forwards to bridge → engine streams
   tokens back → renderer paints them

Key paths inside the packaged `.app`:

```
Freyja.app/
├── Contents/
│   ├── MacOS/Freyja                     # Electron binary
│   ├── Resources/
│   │   ├── app.asar                     # bundled main + renderer
│   │   ├── bridge/                      # Python source (extraResources)
│   │   ├── engine/                      # Python source
│   │   ├── python-bundle/               # bundled runtime (~85 MB)
│   │   │   ├── bin/python3
│   │   │   └── lib/python3.13/...
│   │   └── .env                         # API keys
│   └── Info.plist                       # bundle id: co.freyja.desktop
```

User-side data lives in `~/.freyja/`:

```
~/.freyja/
├── sessions/
│   └── {session-id}/
│       ├── transcript.json              # engine transcript for context restore
│       ├── slice.json                   # UI state for re-mount
│       └── artifacts/                   # subagent outputs
│           └── sub_*.md
└── settings.json                        # permission tier, model defaults, etc.
```

---

## Project layout

```
freyja/
├── src/
│   ├── main/         # Electron main process
│   ├── preload/      # contextBridge to renderer
│   ├── renderer/     # React UI (32 components)
│   └── shared/       # IPC + event types
├── bridge/
│   ├── freyja_bridge.py            # 2k-line stdio bridge — sessions, commands
│   ├── transcript_persistence.py   # transcript save/load
│   └── tools/                      # 17 tool implementations + registry
├── engine/
│   ├── runner.py            # async agent loop
│   ├── session.py           # transcript + tool state
│   ├── compaction.py        # summary-based compaction (artifact-aware)
│   ├── providers.py + *_provider.py   # 4 provider families
│   └── ...
├── native/freyja_native/
│   └── src/                 # Rust pyo3 — capture, input, windows, AX
├── scripts/
│   ├── dev.mjs                # dev orchestrator
│   ├── bundle-python.sh       # Python runtime bundler
│   ├── build-main.mjs         # esbuild for main + preload
│   ├── sign-resources.js      # ad-hoc codesign afterPack hook
│   ├── create-dmg.mjs         # hdiutil DMG builder
│   ├── convert-to-atif.py     # v3 JSON → ATIF v1.6
│   └── convert-to-sharegpt.py # v3 JSON → ShareGPT JSONL
├── docs/
│   ├── ARCHITECTURE.md
│   ├── TRAJECTORY-TRAINING.md
│   └── research/              # design docs, blog posts
└── python-bundle/             # generated by bundle-python.sh
```

---

## Common workflows

### Add a new tool

1. Implement in `bridge/tools/<name>.py` following the `ToolDefinition` /
   `execute()` pattern from existing tools.
2. Register it in `bridge/tools/registry.py:build_desktop_registry`.
3. (Optional) Add it to an agent type's `tool_include` whitelist in
   `bridge/tools/agent_types.py`.
4. Restart the bridge — the renderer learns about new tools via the
   `tool_catalog_entry` event.

### Add a new agent type

One dict entry in `bridge/tools/agent_types.py:AGENT_TYPES`. The parent
agent's system prompt auto-generates from the registry, so no other
files need touching.

### Add a new model

Add an entry to `AVAILABLE_MODELS` in `bridge/freyja_bridge.py` and
ensure `_family_for_model()` routes it to the right provider. The model
picker in the UI populates from this list.

### Update Python deps

```bash
uv add <package>
uv sync
./scripts/bundle-python.sh   # rebundle for the .app
npm run package
```

### Update the native Rust extension

```bash
cd native/freyja_native
uv run maturin develop --release
cd ../..
./scripts/bundle-python.sh   # picks up the new .so
npm run package
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `npm run package` succeeds but app shows "demo mode" | Missing `.env` — check `Resources/.env` exists in the bundle |
| Bridge fails to start in packaged app | `python-bundle/` not built. Run `./scripts/bundle-python.sh` then re-package |
| `freyja_native not found` in dev | Run `maturin develop --release` in `native/freyja_native/` |
| `freyja_native not found` in packaged app | The `.so` wasn't copied. `bundle-python.sh` syncs from `.venv/lib/python*/site-packages/freyja_native` — make sure you ran `maturin develop` in the project venv before bundling |
| Computer-use tools fail with "no permission" | First launch the app, trigger a screenshot, accept TCC prompts. Re-launching after a rebuild may require re-grant because ad-hoc signatures change |
| DMG creation fails on macOS Tahoe | Already handled by `scripts/create-dmg.mjs` — don't use electron-builder's built-in dmg target |
| First launch blocked by Gatekeeper | Right-click the `.app` → Open. After the first allow it launches normally |

---

## License

MIT.

---

*Built on top of the agent-harness engine. See `docs/ARCHITECTURE.md`
for the deeper story.*
