# Freyja — chrome / surface architecture · design brief

A neutral handoff for someone designing Freyja's top-of-app chrome and a few adjacent surfaces. The doc states the problem and the constraints. It does not propose a direction. Take it wherever your taste leads.

## What Freyja is

A macOS desktop app (Electron + React renderer + Python "bridge" backend) for operating one or more conversational AI agents. Users typically have it open for hours at a time — sometimes a single long conversation, sometimes several sessions, sometimes scheduled jobs running unattended in the background. Output can be delivered to channels (Slack, Discord, email, etc) or surfaces on the user's machine (filesystem, notifications). Dark mode only; canonical type is Fraunces (editorial serif, used sparingly) + Geist Mono (everything else).

## What's on screen today (rough inventory)

**Top bar (the TitleBar, ~46px).** Around 14 elements: wordmark + topographic mark, bridge connectivity status, workspace/activity/focus toggles, mission-dashboard button, metrics-dashboard button, model picker, coordination-strategy chips, context-usage readout, spend readout, streaming indicator, session id, scheduler health pill, Slack-gateway health pill.

**Left column.** Workspace + session list. Generally working; the conversation list is the primary navigation.

**Center.** Conversation message stream + input dock. Recently iterated, considered done for now.

**Right column ("activity panel").** A mix of two kinds of content under one heading: live per-turn activity (tool timeline, sub-agents fired, findings from the message bus, file changes during the turn, drafter/skill candidates from the in-flight session) and stable inventories that don't change with the turn (skills index, memory entries).

**Surfaces summoned by hotkey or button.** Mission Dashboard (⌘⇧M), Scheduled Jobs (⌘⇧S), Metrics Dashboard, Settings, Command Palette (⌘K), Kanban, Quick Switcher (Ctrl+Tab), DebugDrawer (⌘D). Some of these are very well-developed; some less so. A "Gateways" surface for integrations does not yet exist.

## Problems

These are observations, not requirements. Some may not be problems to you.

1. **Top bar real estate has been treated as a parking lot.** Every new feature added a pill. There's no explicit rule for what earns permanent screen space vs what should be summoned.

2. **Several pills show static metadata, not feedback to the user's action.** "Bridge: live" reads "live" 99.99% of the time. The Slack pill reads "live" when Slack is connected (which it almost always is). The scheduler pill shows a count + countdown for jobs the user has already configured. These take constant cognitive attention for low information return.

3. **Activity panel mixes live and stable content.** "Skills" and "Memory" are catalogs you browse occasionally; they're not activity. They ended up there because there wasn't a better home.

4. **Discoverability of features is uneven and inconsistent.** Some surfaces are reachable from a pill, some from a hotkey only, some from a slash command, some from a Command Palette entry, some only by drilling into the Mission Dashboard. There is no single canonical entry path. The user has flagged this explicitly — e.g. there's no obvious way to reach a diff/changes viewer that exists in the code.

5. **The integration count will grow.** Today there's one channel integration shown in the chrome (Slack). Plausible 12-month roadmap: Discord, Email, Telegram, Notion, Linear, GitHub webhooks, Calendar, custom HTTP webhooks, plus multiple model providers (Anthropic, OpenAI, Gemini, Ollama) and multiple tool integrations (Computer use, Browser MCP, Filesystem). The current pattern of one chrome pill per integration will not scale.

6. **The new-session view is generic.** When the user opens a fresh session they see a centered welcome card + a button. The decisions that matter at session start — which model, which coordination strategy (this one *locks* at first send), what permission tier, what skills to preload — aren't surfaced at the moment they can still be chosen.

7. **Compaction is invisible in the chrome.** When the agent compacts context mid-turn (a real and consequential event) nothing in the UI reflects it. Context pressure climbing is also currently invisible; whether that needs to be visible at all is an open question if compaction is reliable.

8. **Diagnostics is buried.** Bridge events, logs, errors, daemon health, and gateway traffic all exist in the data, but the user-facing surface for them is a small slide-out drawer reached by ⌘D. Many users don't know it exists.

9. **Focus mode hides everything.** Including ctx, spend, streaming state, and cancel-turn. In long sessions where the user wants chrome out of the way, they also lose all signals about cost and progress.

## Constraints

- macOS-first; drag region in the top bar matters; the leftmost ~82px is reserved for the traffic-light buffer.
- Dark mode only for now.
- The existing palette (⌘K), hotkeys, slash commands, and the Scheduled Jobs / Kanban / Mission Dashboard / Settings surfaces work and should not be re-invented from scratch.
- The bridge already pushes all the signals you'd want (tool calls, streaming state, sub-agent count, permission requests, compaction events, scheduler runs, gateway events). Verb / state derivation can be a thin selector on top of existing state; new bridge messages aren't required.
- React + Zustand for the renderer.

## Out of scope

Don't redesign the conversation message stream, the sidebar's session list, the input dock, the Kanban board, the Scheduled Jobs modal, or the Settings modal. These are recently shipped and intentionally stable.

## Open questions for the next designer

- Should context pressure be visible at all, given compaction works? Or is the only ctx-event worth showing the moment compaction fires?
- Should skills + memory leave the activity panel? If so, where do they go?
- Does the user need at-a-glance status of integrations / gateways, or is "I'll know when something fails because the action will fail" acceptable for set-and-forget connections?
- Should the new-session view be a redesign or just config chips on top of the existing welcome?
- Should diagnostics become a first-class surface or stay a drawer?
- Should focus mode preserve any signals (ctx, spend, cancel) or truly hide everything?
- How should the chrome scale as the integration count goes from ~5 to ~20 over the next 12 months?

That's the territory. Where you land is up to you.
