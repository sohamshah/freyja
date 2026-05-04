# Freyja Performance Deep Dive

Date: 2026-04-30

This audit focuses on the symptoms that show up when many sub-agents/sub-sessions are active, and when long sessions accumulate images or computer-use screenshots:

- machine heat and high CPU during swarm runs
- input typing/click lag in the app
- renderer stutter after long image-heavy sessions
- slow completion / restart behavior as session count grows

The short version: pagination/virtualization is needed, but it is not enough by itself. The current hot path combines high-volume agent fanout, per-event renderer state updates, whole-history React rendering, inline base64 image transport, and synchronous all-session persistence.

## Executive Summary

The biggest performance issue is that "live UI state" and "persistent transcript state" are currently the same heavy object graph.

Screenshot frames move as base64 strings from Python stdout, through Electron main, through IPC, into Zustand, into React `img src=data:...`, and finally into pretty-printed JSON session files. That creates repeated large string copies, image decodes, synchronous serialization, and long-lived memory retention.

The second biggest issue is fanout. A single parent turn can spawn many sub-agent sessions, each with its own model stream and tool stream. The UI then processes each child event individually and updates either the active slice or `sessionArchive`, causing broad React subscriptions to re-evaluate.

The third issue is no windowing. The conversation maps every message on every render, multiple sidebar/swarm views map every session/subagent, and search/scroll effects repeatedly inspect layout or DOM nodes across a growing transcript.

Observed local data supports this: `~/.freyja/sessions` currently contains 213 UI session JSON files totaling about 191 MB, with 1,119 persisted `pngBase64` frame entries. The largest UI session JSON is about 16.5 MB and contains 62 frame payloads; several computer-use sessions are 10-13 MB each with roughly 90-100 frame payloads.

## 2026-04-30 Idle Main Page Profile Update

A separate idle-main-page profile found that the app can burn CPU/GPU without any active agents.

Observed running `/Applications/Freyja.app` on the empty/main page:

- Freyja renderer helper: roughly 101% CPU.
- Freyja GPU helper: roughly 68-79% CPU.
- WindowServer: roughly 44-68% CPU.
- Freyja main process and Python bridge: 0% CPU.

Samples were written to:

- `/private/tmp/freyja-idle-renderer-39661.sample.txt`
- `/private/tmp/freyja-idle-gpu-39658.sample.txt`

The renderer sample showed active Electron/V8 work, and the GPU sample was dominated by `glStartTilingQCOM`, AGX Metal render-context work, and Quartz/CoreAnimation commit paths. That matches a continuous visual/compositor workload rather than agent execution, persistence, or bridge I/O.

Two idle causes were confirmed:

1. `TopoWordmark` redrew a heavy contour-line canvas through `requestAnimationFrame` forever. It now animates on initial display and interaction, then settles into a static canvas.

2. Full-window and structural-panel CSS `backdrop-filter` kept the GPU compositor active even after the renderer became mostly idle. A clean patched-build validation showed the renderer drop from about 100% to about 5%, but the GPU helper still sat around 48% until CSS backdrop filters were removed from the always-on `.app-tint` and `.glass` surfaces. Disabling only those large surfaces dropped the validation GPU helper to about 13-14%; disabling all backdrop filters dropped it to about 7%, confirming the remaining gap is smaller raised/modal glass.

The source patch keeps native macOS window vibrancy enabled and preserves CSS backdrop filters on smaller emphasis surfaces such as `.glass-strong`, `.glass-raised`, and `.glass-chip`.

## Constraints

The fixes in this document should preserve the current product and agent semantics:

- Do not reduce the number of sub-agents the system can run.
- Do not reduce computer-use max steps.
- Do not reduce how many screenshots can be captured.
- Do not hide screenshots, remove inline affordances, or make the live computer-use experience less informative.
- Do not make computer-use artificially sequential if the current product allows multiple active sessions.
- Do not solve renderer lag by dropping user-visible events or losing historical state.

The right target is to move heavy work off hot paths and make the UI lazy, indexed, and backpressured without changing what the agents can do.

## Performance Model

Each live bridge event currently goes through this chain:

1. Python bridge emits one JSON line and flushes stdout immediately.
2. Electron main appends/parses stdout chunks and calls `webContents.send`.
3. Preload dispatches the event to every registered listener.
4. Renderer calls `handleEvent(event)`.
5. Zustand folds the event into a session slice.
6. React subscribers render based on changed slices and broad selectors.
7. Some components do layout/DOM work after render.
8. On turn/session completion, the renderer persists every known session.

That chain is reasonable for small text-only sessions. It breaks down under these multipliers:

- N sub-agents streaming at once
- each sub-agent generating text deltas, tool deltas, tool result events, usage events, and terminal events
- computer-use sessions adding 100-300 KB base64 image frames per capture
- old session slices staying resident in `sessionArchive`
- completion events triggering all-session serialization

## Hot Path 1: Event Transport Has No Batching

Relevant code:

- `bridge/freyja_bridge.py:116-123` emits every event with `json.dumps`, newline, and immediate `stdout.flush()`.
- `src/main/bridge.ts:289-303` parses each stdout JSON line and calls `emit(obj)`.
- `src/main/main.ts:119-122` immediately forwards each event to the renderer with `webContents.send`.
- `src/preload/preload.ts:8-15` loops through listeners synchronously.
- `src/renderer/App.tsx:97-108` calls `handleEvent(event)` for every event and kicks persistence on terminal events.

There is no frame/turn-level coalescing. Text deltas, thinking deltas, tool input deltas, and screenshot frames all arrive as separate state updates. Under a swarm, this can become many updates per animation frame.

Recommended fix:

- Add a renderer-side event queue that flushes at most once per animation frame.
- Preserve strict event order per session, but coalesce adjacent `text_delta`, `thinking_delta`, and `tool_input_delta` events before folding them into state.
- Preserve every `screenshot_frame`, but register frames as media records and batch React-visible state commits. The live view can update at display frame rate while the historical frame list remains complete.
- Later, push batching upstream into main/preload so the renderer receives a small batch IPC payload instead of many one-event IPC messages.

## Hot Path 2: Renderer State Updates Copy Growing Structures

Relevant code:

- `src/renderer/state/store.ts:390-423` maps the full `messages` array on every text/thinking delta.
- `src/renderer/state/store.ts:452-483` copies the full `toolCalls` object for tool start/input events.
- `src/renderer/state/store.ts:487-529` copies `toolCalls` and sometimes `artifacts` on every tool result.
- `src/renderer/state/store.ts:1097-1120` updates non-active child sessions by rewriting `sessionArchive[sessionId]` for every child event.
- `src/renderer/state/store.ts:847-945` stores screenshot frames on both `computerSessions[sessionId].latestFrame` and the latest tool call record.

The model is immutable, which is good for correctness, but the changed objects are coarse. Broad subscribers then see new object identities and re-run expensive derivations.

Recommended fix:

- Split live state into narrower stores or use narrower Zustand selectors with shallow equality.
- Keep media bytes outside the session slice.
- Store tool calls in a structure that lets a single call update avoid invalidating consumers that subscribe to the whole `toolCalls` object.
- Add a batch reducer (`handleEvents(events)`) so 20 stream deltas become one state mutation.

## Hot Path 3: Whole-Transcript Rendering

Relevant code:

- `src/renderer/components/Conversation.tsx:19-22` subscribes to `messages`, `thinking`, and `isStreaming`.
- `src/renderer/components/Conversation.tsx:201-203` renders `messages.map(...)` with no virtualization.
- `src/renderer/components/Conversation.tsx:212-221` makes every `MessageView` subscribe to global streaming state.
- `src/renderer/components/Conversation.tsx:348-356` renders markdown for text parts during render.
- `src/renderer/components/Conversation.tsx:57-72` runs a DOM-wide `.search-hit` query after every message/thinking update, even when no search is active.
- `src/renderer/components/Conversation.tsx:133-160` reads `scrollHeight` and sets `scrollTop` on every message/thinking/streaming change when sticky-scroll is enabled.

This directly explains typing/click lag after long sessions: even if the input component itself is small, the app shares the same renderer main thread with a large transcript DOM, repeated markdown rendering, search scans, image decode work, and layout work.

Recommended fix:

- Virtualize/window the conversation. Render a bounded window around the viewport plus the active streaming tail.
- Keep an explicit "load older messages" affordance or top sentinel for pagination.
- Memoize `MessageView` and change streaming selection to `s.isStreaming && s.currentStreamingMessageId === message.id`, so non-streaming messages do not re-render on global stream state changes.
- Cache rendered markdown per message part revision.
- Skip the search hit scan entirely when `searchQuery` is empty.
- Throttle auto-scroll layout reads/writes to animation frames.

Pagination helps most here, but it does not fix the persistence or base64 transport costs by itself.

## Hot Path 4: Inline Base64 Media Is the Largest Multiplier

Relevant code:

- `src/shared/events.ts:79-85` stores `ToolCallRecord.frame.pngBase64`.
- `src/shared/events.ts:303-310` defines `screenshot_frame.pngBase64`.
- `bridge/tools/computer_tools.py:321-337` base64-encodes every emitted frame into the event.
- `src/renderer/state/store.ts:866-873` stores the base64 frame in renderer state.
- `src/renderer/state/store.ts:900-903` attaches the frame to the latest tool call.
- `src/renderer/components/ToolCallChip.tsx:71-79`, `ParallelToolGroup.tsx:205-210`, `SubagentCard.tsx:143-153`, and `ComputerLiveView.tsx:112-154` turn base64 strings into data URLs for images.
- `src/renderer/state/store.ts:1371-1379` stores pasted/dropped image attachments as base64 data URLs.
- `src/renderer/state/store.ts:1144-1146` persists attachment preview data URLs into user messages.

This creates copies at every layer:

- screenshot bytes
- base64 Python string
- JSON stdout line
- Node string buffer
- parsed JS string
- IPC structured clone
- Zustand state string
- React data URL string
- decoded image bitmap
- persisted JSON string

For computer-use sessions, the app also stores many historical frames by attaching each one to a tool call. Historical frames are useful and should be preserved, but their bytes should not live inside the transcript object graph.

Recommended fix:

- Introduce a media store. Events should carry `frameId`, `mimeType`, dimensions, and display metadata; bytes should move through a media channel or cache, not through the transcript slice.
- Write every frame to a cache directory or serve it through a local `app://`/custom protocol/blob URL. This preserves screenshot count and history while removing base64 strings from React state.
- Keep live frame handles in memory, not image bytes. Historical frame metadata stays available immediately; full bytes load lazily when the frame is actually rendered.
- Persist frame metadata and media references. Do not persist `pngBase64` in UI session JSON.
- Generate thumbnails as additional cached media for collapsed views; keep full-resolution frame media available on expansion.
- Revoke object URLs when sessions unload.

Minimum immediate fix:

- Do not strip `toolCalls[*].frame` from persisted session JSON until the media store exists and restart hydration can resolve frame references back into images.
- First remove the `persistAllSessions()` hot path and the renderer-side JSON deep clone so existing screenshot behavior stays intact.
- Keep inline screenshots enabled; when the media store lands, change their source from inline base64 to cached media URLs.

## Hot Path 5: Persistence Is Synchronous and O(all sessions)

Relevant code:

Original hotspot:

- `src/renderer/App.tsx` called `persistAllSessions()` after every `turn_complete` or `session_completed`.
- `src/renderer/state/store.ts` looped over every known session and saved each non-empty slice.
- The renderer deep-cloned each slice with `JSON.parse(JSON.stringify(slice))`.
- `src/main/persistence.ts` pretty-printed the whole session and wrote it with `fs.writeFileSync`.
- `src/main/main.ts` handled `session:save` by calling the synchronous save function.

This was a strong match for "too many subsessions are active" and "long running sessions lead to issues." A swarm completion can emit terminal events for many children. Each terminal event could trigger `persistAllSessions`, and each call serialized every known session. With 50 sessions, that became repeated O(50 * total-session-size) work.

Pretty JSON also inflates files and costs CPU. Local evidence shows UI JSON files totaling about 191 MB, largely driven by persisted frames.

Recommended fix:

- Replace `persistAllSessions()` on every terminal event with dirty-session persistence.
- Persist only the session that changed, plus a small session metadata/lineage index.
- Debounce saves per session, for example 1-2 seconds while active and immediate on app quit.
- Move serialization to a worker or at least use async filesystem calls in main.
- Stop pretty-printing large session slices. Use compact JSON for machine state and keep human-readable trace export as the explicit export path.
- Persist media separately and store references once the media store is in place. Until then, keep frame payloads in the full UI slice so restart behavior does not regress.

Suggested target design:

- `sessions-index.json`: lightweight snapshots and parent/child links.
- `sessions/{id}.ui.json`: media-stripped UI slice.
- `sessions/{id}.transcript.json`: engine transcript, already compacted separately by the bridge.
- `media/{frameId}.jpg`: durable media cache. If cleanup is added later, it should archive or compact media without silently removing frames from sessions.

## Hot Path 6: Full Concurrency Exposes Local Overhead

Relevant code:

- `bridge/tools/sub_agent_tool.py:56` sets `MAX_ACTIVE_SUBAGENTS = 30`.
- `bridge/tools/sub_agent_tool.py:237-239` schedules background sub-agents with `asyncio.create_task`.
- `engine/types.py:818-823` defaults to `parallel_tool_execution=True` and `max_parallel_tools=10`.
- `engine/runner.py:1136-1166` executes multiple model-requested tool calls concurrently.
- `bridge/tools/subagents_tool.py:158-168` `wait_all` waits for all running background sub-agents with no timeout.
- `bridge/tools/computer_use_tool.py:48-53` allows up to 60 default computer-use iterations and 2 active computer sessions.
- `bridge/tools/computer_use_tool.py:300-302` lets a caller request up to 200 computer-use steps.

The concurrency cap is per concept, not a global resource budget. A parent can run parallel tool calls, some of those can spawn sub-agents, and each child can run its own model/tool loop. Under the constraint for this audit, that full concurrency should remain available. The local problem is that UI, persistence, media transport, logging, and input/capture I/O scale poorly with that intended concurrency.

Recommended fix:

- Preserve existing execution limits and semantics.
- Add instrumentation that separates real agent/tool work from UI overhead:
  - event counts by type/session
  - JSON/IPC byte volume
  - renderer reducer time
  - persistence time
  - screenshot capture/encode time
  - image decode/render time
- Move UI event ingestion, media persistence, and heavy serialization off the renderer-critical path so the same number of agents produces fewer UI stalls.
- If a scheduler is introduced later, use it for fairness and resource accounting, not for lowering default capability.

## Hot Path 7: Computer-Use I/O Has Expensive Per-Action Work

Relevant code:

- `src/main/inputProxy.ts:98-142` shells out to a new Python process via `execFileSync` for every input action.
- `src/main/inputProxy.ts:180` runs that synchronous subprocess inside the HTTP request handler.
- `src/main/captureProxy.ts:66-78` requests native display-sized thumbnails from `desktopCapturer`.
- `src/main/captureProxy.ts:99-120` then resizes and encodes the image.
- `bridge/tools/computer_tools.py:191-337` captures, cursor-composites, optionally dumps to disk, base64-encodes, and emits every frame.
- `bridge/tools/computer_tools.py:55-61` enables frame dumping by default unless `FREYJA_FRAME_DUMP` is explicitly false.
- `bridge/tools/computer_tools.py:1232-1237`, `1345`, `1526`, `1633`, and `1892` capture a fresh screenshot after mutating actions.

The most suspicious single I/O issue is `execFileSync` per input action. Starting Python for every click/key/scroll is expensive and blocks Electron main while the request is handled. During computer-use loops, that can directly cause clicks, typing, and app UI responsiveness to lag.

Recommended fix:

- Replace per-action `execFileSync` with a persistent helper process or direct native module call.
- Queue input actions through one worker so Electron main never blocks on process startup.
- Capture scaled thumbnails at source when `max_dim` is known instead of requesting full native pixel size and resizing afterward.
- Keep frame dumping semantics if they are useful, but make debug writes asynchronous and outside the capture/action latency path.
- Add non-dropping screenshot backpressure: persist every frame to the media store, but decouple media ingestion from React rendering so the renderer can catch up without losing frame history.

## Hot Path 8: Visual Effects and Spinners Add Background Cost

Relevant code:

- `src/renderer/lib/spinner.tsx:16-34` creates one `setInterval` per mounted spinner.
- Many running subagent/tool rows render spinners: `SubagentCard`, `SubagentSwarmGrid`, `SwarmMonitor`, `Sidebar`, `ToolTimeline`, etc.
- `src/renderer/components/TopoWordmark.tsx` previously used an unbounded `requestAnimationFrame` loop on the empty main page.
- `src/renderer/styles/globals.css` previously applied `backdrop-filter` to the full-window tint and large structural panels.
- `src/renderer/styles/globals.css` still applies CSS backdrop filters to smaller emphasis surfaces where the visual payoff is more localized.

The design is visually heavy by intent. That is fine for normal workloads, but a swarm can render many glass cards and many independent interval-driven spinners. On long sessions, those effects compound with the transcript DOM and image decoding.

Recommended fix:

- Replace per-spinner intervals with one shared animation clock or CSS-only animation.
- Preserve the same visual affordances, but implement them with fewer timers and fewer repeated paint invalidations.
- Add CSS containment/content-visibility where it does not change layout semantics.
- Avoid re-decoding identical image sources by stabilizing image nodes and using cached media URLs.
- Make decorative canvas animations finite or visibility/interaction-driven.
- Keep native vibrancy as the large-surface glass layer; avoid full-window/sidebar-sized CSS backdrop filters while the app is idle.

## Secondary Hot Spots

Artifact workspace:

- `src/renderer/lib/useArtifactMeta.ts:51-102` reads metadata with 6 concurrent IPC calls and periodically flushes the full cache to state.
- `src/renderer/components/ArtifactPreview.tsx:178-245` formats/highlights entire JSON content into many React elements.
- `src/renderer/components/ArtifactPreview.tsx:249-294` renders every CSV row.

Command palette:

- `src/renderer/components/CommandPalette.tsx:30-82` builds commands, skills, subagents, and sessions into one list.
- `src/renderer/components/CommandPalette.tsx:84-94` filters the whole list on every query change.

Sidebar:

- `src/renderer/components/Sidebar.tsx:75-102` rebuilds the full session tree whenever `sessions` changes.
- `src/renderer/components/Sidebar.tsx:163-239` maps all child sessions in the swarm section.

These are not the first fires to put out, but they should be virtualized/windowed once the main issues are handled.

## Prioritized Fix Plan

### P0: Stop the Bleeding

1. Remove all-session persistence from terminal events.
   - Persist only the session that changed.
   - Persist lineage/session metadata separately from full UI slices.
   - Keep existing frame payloads in session files until media references are fully implemented.

2. Move screenshot/media bytes out of session state.
   - Every screenshot remains captured and reviewable.
   - The session slice stores frame ids and metadata.
   - React image elements load from cached media URLs, not base64 data URLs.

3. Add renderer event batching.
   - Coalesce text/thinking/tool-input deltas.
   - Preserve every screenshot as a media record.
   - Batch state commits without dropping events.

4. Fix the worst React invalidations.
   - Memoize `MessageView`.
   - Narrow streaming selectors to per-message booleans.
   - Skip search DOM scans when no query is active.

5. Remove blocking input/capture work from Electron main.
   - Replace per-action Python process startup with a persistent helper or native path.
   - Make debug frame writes asynchronous.

### P1: Structural Fixes

1. Implement conversation virtualization/pagination.
   - Render a bounded message window.
   - Keep streaming tail mounted.
   - Add "load older" or scroll-top pagination.

2. Create a real media store.
   - Frame events carry handles, not bytes.
   - Object URLs or app protocol URLs feed images.
   - Full frame bytes load only on demand.

3. Move persistence further off the hot path.
   - Dirty-session debounce.
   - Async writes.
   - Compact JSON.
   - Dedicated index for session metadata.

4. Replace input proxy subprocess-per-action.
   - Persistent worker or native binding.
   - Main process should not block while injecting input.

5. Add non-dropping backpressure between ingestion and rendering.
   - Accept all bridge events.
   - Persist complete media/history.
   - Let React render at display cadence instead of event cadence.

### P2: Instrumentation and Hardening

1. Add performance telemetry in development builds:
   - event count by type/session
   - IPC payload byte estimate
   - reducer time by event type
   - React render count for conversation/message/tool components
   - persistence duration and payload size
   - screenshot capture/encode duration
   - input action latency

2. Add a long-task observer in the renderer.

3. Add a maintenance command to prune persisted frame payloads from old session JSON.

4. Add thresholds that warn when UI overhead, persistence, or media transport exceeds budget. Warnings should diagnose; they should not silently reduce sub-agent count, screenshot count, or computer-use steps.

## Why Pagination Alone Is Not Enough

Pagination fixes the DOM and React render cost of old messages. It will help typing, scrolling, and click responsiveness in long sessions.

It will not fix:

- base64 frames being copied through stdout, IPC, state, and JSON
- remaining persistence work such as debounce/app-quit flushing and future worker serialization
- Electron main blocking on `execFileSync` input actions
- computer screenshots being captured/encoded/dumped too aggressively
- the full intended model/tool/sub-agent fanout still creating high event and media volume

So pagination should be part of the fix, but the first architecture boundary should be: media bytes are not transcript state.

## Implemented Patch Sets

Implemented in the first patch set:

1. Replaced terminal-event `persistAllSessions()` with targeted `persistSession(sessionId)` calls.

2. Added a lightweight session metadata index for sidebar hydration and parent/child lineage.

3. Removed the renderer-side JSON deep clone before persistence.

4. Changed main-process session saves to async compact JSON with timing/size logging.

Implemented in the renderer hot-path patch set:

1. Added a media cache/reference path for screenshot frames.
   - Preserve every captured screenshot.
   - Keep inline screenshots visible.
   - Replace data URLs with cached media URLs.

2. Added a renderer event queue in `src/renderer/App.tsx`.

3. Memoized `MessageView`, narrowed active streaming rendering, skipped empty search scans, lazy-decoded inline images, and added content visibility for old rows.

Implemented in the idle-main-page patch set:

1. Changed `TopoWordmark` from an infinite canvas RAF loop to finite load/interaction drift.

2. Removed CSS `backdrop-filter` from the full-window `.app-tint` and large structural `.glass` surfaces, relying on native macOS vibrancy for those layers.

Remaining high-confidence next patch:

1. Replace per-action `execFileSync` input injection with a persistent helper or direct native call.

2. Add conversation virtualization/pagination for very long sessions.

This would not be the final architecture, but it should noticeably reduce heat and lag quickly.

## Operational Mitigations Until Code Changes

These are maintenance actions that do not reduce agent capability:

- Prune or archive old `~/.freyja/sessions/*.json` files that are dominated by persisted frame payloads.
- Restart the app after unusually large image-heavy runs to clear retained renderer memory until media handles replace inline base64.

## Final Diagnosis

The core issue is not just rendering. It is that high-frequency live data, large media payloads, persisted UI history, and React render state share the same path.

The durable fix is to split those concerns:

- event stream: batched and backpressured
- live UI state: small and current
- transcript state: text/tool metadata only
- media state: handle-based, lazy, complete
- persistence: dirty, debounced, async, media-free
- execution: same capability envelope, but decoupled from UI/persistence/media hot paths

Once that split exists, pagination becomes straightforward and effective instead of trying to compensate for oversized state and I/O.
