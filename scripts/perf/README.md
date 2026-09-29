# Renderer performance harness

Tools for measuring how the desktop renderer behaves on a long, real
session: typing, scrolling, clicking, opening, and streaming. They run the
built renderer in Chrome with a stand-in bridge (`mock-harness.js`) that
serves your own `~/.freyja/sessions` files, and replay real bridge events
from those sessions' event logs.

Everything under `out/` is git-ignored. Replays and profiles contain real
session content, so never commit them.

## Setup

```sh
cd scripts/perf
npm install                       # playwright-core + source-map-js
# Build the renderer unminified so profiles have real function names:
npx vite build --outDir "$PWD/out/dist" --minify false --emptyOutDir
```

`profile.mjs` drives your installed Google Chrome.

## Measure

```sh
# Streaming workload: the densest 30 s of the session's own stream plus
# its 8 busiest sub-agents.
python3 build_replay.py --session <session-id> --out out/replay.json
# Sub-agents only (parent idle, waiting on a swarm):
python3 build_replay.py --session <session-id> --no-parent --children 10 --out out/replay-kids.json

node profile.mjs --dist out/dist --title "<session title>" \
  --scenarios open,type,scroll,click,stream,streamtype --out out/run.json
```

Each scenario reports frame gaps, long tasks, input latency (Event Timing),
the renderer's busy time (`busy`: script, layout, style, and total task
seconds), DOM size, and the number of store subscriptions mounted.

- `--cpuprofile out/prefix` writes a `.cpuprofile` per scenario. Summarize
  one with `node analyze_profile.mjs <file> 40 --map out/dist/assets/index-*.js.map`.
- `--trace 1` records a Chrome trace and prints main-thread time by event
  (Layout, Paint, HitTest, Layerize, ...).
- `--replay <file>` picks the stream workload; `--speed 2` plays it faster.

`verify.mjs` checks behavior rather than speed: the transcript opens at the
bottom on a tail window, scrolling up reveals everything, search reaches
the oldest message, sidebar folding works, and the tool timeline tooltip
shows on hover.

`check-composer.mjs` checks the composer's auto-grow against the old
measure-by-collapsing method, that the side panels are layout-contained,
and that overlays opened from them still cover the window.

`dom-breakdown.mjs` prints DOM node counts per panel.

The harness runs your installed Chrome, which is newer than the app's
Electron. Some costs only show in the app — a full-window layout on every
change (fixed by containing the side panels) was ~15 ms there and near zero
here — so confirm layout and frame costs with the live scripts below.

## Profiling the running app

The installed app ships with Electron's inspect fuse on, so `kill -USR1
<main pid>` opens a Node inspector on `127.0.0.1:9229` without a restart.
`mainrpc.mjs` evaluates a script inside the main process through it, and
the scripts it runs attach to the window's debugger:

```sh
kill -USR1 "$(pgrep -f 'Freyja.app/Contents/MacOS/Freyja$')"
node mainrpc.mjs live-capture.js   # 25 s renderer CPU profile + main-loop stalls + events/s
node mainrpc.mjs -e "globalThis.__fpWaitForEvents = 5; $(cat live-capture.js)"   # wait for a turn to stream first
node mainrpc.mjs live-trace.js     # 4 s trace: main-thread time by event (Layout, Commit, ...)
node mainrpc.mjs live-traffic.js   # bridge events/s by type, save timings
node mainrpc.mjs live-fibers.js    # store subscriptions per component
node mainrpc.mjs -e "setTimeout(() => require('inspector').close(), 200)"
```

`live-drive.js` measures typing and streaming in the running app without
a model turn: it forks a session (parent only, no project copy), replays
recorded stream events into the fork, and types into the composer (never
Enter), then restores your draft and switches back:

```sh
node mainrpc.mjs -e "globalThis.__fpPhase='fork'; globalThis.__fpSource='<session id>'; $(cat live-drive.js)"
node mainrpc.mjs -e "globalThis.__fpPhase='measure'; globalThis.__fpFork='<fork id>'; globalThis.__fpReplay='$PWD/out/replay.json'; globalThis.__fpTypeMs=25000; $(cat live-drive.js)"
node mainrpc.mjs -e "globalThis.__fpPhase='restore'; globalThis.__fpSource='<session id>'; $(cat live-drive.js)"
```

The fork stays in the sidebar (named "perf-test fork (safe to delete)").

Each script removes its probes and detaches when it finishes.

## Comparing Electron versions

`electron-ab.cjs` runs a built renderer inside a given Electron binary and
reports layout per keystroke / per streamed token, the per-frame commit
cost, and key-to-paint latency. `FP_WINDOW=app` uses the app's
transparent + vibrancy window; `FP_BLUR=1` measures with the window
unfocused. It opens a visible window for about a minute.

```sh
"<path to Electron.app>/Contents/MacOS/Electron" electron-ab.cjs <dist dir> "<session title>"
``` Close the
inspector afterwards: while it is open, any local process can run code in
the app. The installed bundle is minified; extract its source map with
`npx @electron/asar extract <app.asar> out/asar` and pass it to
`analyze_profile.mjs --map`.
