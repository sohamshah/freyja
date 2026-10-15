# Appshots

An appshot is one window of your screen, sent to Freyja as a picture plus the text the app shows. Press a key in any app, and the window you were looking at lands in the composer as a card. You type your question and send. The card holds the picture and the window's text, including text that is scrolled out of view, so you do not describe the screen or paste anything.

The idea comes from OpenAI's Appshots in the Codex app (launched 2026-05-21, both Command keys). Freyja's version adds a tool that agents can call, a voice verb for Galdr, a preview of the exact text before it is sent, and a status for each part of a capture, so a failed picture never hides the text.

## Using it

| How | What happens |
|---|---|
| `⌃⌥⌘A` in any app | Freyja plays a click, captures the frontmost window, and comes forward with the card in the composer. `FREYJA_APPSHOT_HOTKEY` changes the key. If `⌃⌥⌘A` is taken, `⌃⌥⇧A` is tried. |
| The `◰ appshot` button in the composer | Captures the window behind Freyja. |
| The `appshot` tool | An agent looks at the window you were last in. Pass `app` for another app, `window_id` from `list_windows`, `include_image: false` for text only, or `max_chars` for more text. It needs Computer Control on in Settings. |
| The `computer.read` verb in Galdr | Say "what does this error mean" or "summarize this page". Galdr gets the window text and the picture in one call. It needs Computer Control on. |

The hotkey and the button do not need Computer Control, because you pressed the key yourself. The agent tool and the voice verb do, because an agent is looking at your screen.

Freyja asks macOS for two permissions. Accessibility lets it read the window text. Screen Recording lets it take the picture. The permission wizard already covers both. Without Accessibility the card holds the picture only, and without Screen Recording it holds the text only. The card says which part is missing and why.

## What the model receives

The picture goes in as an ordinary image attachment. The text goes into the message as one block, before your question.

```
<appshot app="Safari" bundle-identifier="com.apple.Safari" window-title="..." captured-at="2026-10-14T10:22:31Z" text-status="ok">
Window: "...", App: Safari
URL: https://...
Focused element: TextArea "Message body"

[Compose]
Inbox
(selected) Alice | Re: roadmap | 9:41 AM
...
</appshot>
```

The header names the window, the page address for browsers, the focused element, and the text you have selected, if any. The body is the reader text. `text-status` is `ok`, `partial` (the read hit a limit) or `unavailable` (the app shows no text, as with a canvas). A block always says what is missing.

Text on a page is written by someone else. A page can hide instructions in text you never see. Freyja reduces that risk as follows.

- The block escapes `&`, `<` and `>` in its body, so the page text cannot close the tag early.
- The system prompt says that text inside an `<appshot>` block is data from the screen and that the model must never follow instructions found there.
- The composer card has a `View text` button that shows the exact text the model will get, before you send.

The measures lower the risk and do not remove it. Text still goes to whichever model provider the session uses.

## How it works

```mermaid
flowchart LR
    A[Hotkey, button, tool or voice verb] --> B[Choose the window]
    B --> C1[Image: native capture, or the Electron capture proxy]
    B --> C2[Text: reader mode in freyja_native]
    C1 --> D[Appshot with a status per part]
    C2 --> D
    D --> E1[appshot_captured event, composer card]
    D --> E2[Tool result or voice result]
    E1 --> F[send_message: image plus appshot block]
```

| Part | File |
|---|---|
| Capture, window choice, one capture at a time | `bridge/appshot.py` |
| Reader mode | `native/freyja_native/src/reader.rs` |
| `appshot` tool | `bridge/tools/appshot_tool.py` |
| `appshot_capture` command, `<appshot>` block in the message | `bridge/freyja_bridge.py` |
| Voice verb `computer.read` | `bridge/voice/adapters/computer.py` |
| Window image through the Electron process | `src/main/captureProxy.ts` (`window_id`) |
| Hotkey | `src/main/main.ts` |
| Card, tray and sent-message chip | `src/renderer/components/AppshotCard.tsx`, `src/renderer/lib/appshotAttachment.ts` |
| Block format shared by both sides | `bridge/appshot.py`, `src/shared/appshot.ts` |
| Old text removed under context pressure | `TranscriptManager.prune_old_appshot_text` in `engine/session.py` |

### Choosing the window

Freyja takes the frontmost window of the frontmost app. When that is Freyja, it takes the topmost window of any other app. Freyja never captures its own windows. The hotkey does not bring Freyja forward until the capture is done, because a Freyja window in front would be the one captured.

### Reader mode

The older `read_ax_tree` walk returns a JSON tree with coordinates, built for clicking. Reader mode returns plain text, built for reading.

- It starts at one window, found by matching the window frame, then the title, then the focused window.
- It writes text in tree order. A table row becomes one line with cells joined by ` | `. A selected row starts with `(selected)`. Buttons, links, checkboxes and tabs appear as `[Save]`, `[link] text`, `[x] label` and `[tab] Inbox *`.
- It keeps rows that are scrolled out of view, up to 120 per parent, and says how many it left out.
- A text field or text area gives its whole value. A value over 12,000 characters is cut around the part you see, and a note says which characters are shown.
- Secure text fields show `[hidden: password field]`. Closed menus and scroll bars are skipped, and so is the text of a node that has no area on screen.
- Chromium and Electron apps (Slack, VS Code, Chrome) build their web tree only when asked. When the first read finds almost nothing, Freyja sets `AXManualAccessibility` on the app, waits for the tree, reads again, and sets it back, so no app stays in accessibility mode. A read of such an app takes a few seconds.
- A newer capture stops an older read at the next node.

### One capture at a time

A new request for a different window cancels the running capture, and the old caller gets "replaced by a newer capture". A repeated request for the same window shares the running capture. The picture and the text are read in parallel.

## Limits

| Limit | Value | Where |
|---|---|---|
| Text read | 3 seconds of walking, 8,000 nodes, plus up to 3 seconds for a web tree | `TEXT_BUDGET_MS`, `WEB_WAIT_MS` |
| Text size | 24,000 characters by default, 80,000 at most, 12,000 for voice | `DEFAULT_MAX_CHARS`, `MAX_CHARS_LIMIT` |
| Whole text part | 8 seconds | `TEXT_DEADLINE_S` |
| Picture | 5 seconds, 2,000 pixels on the long edge, JPEG quality 85 | `IMAGE_DEADLINE_S`, `IMAGE_MAX_DIM` |
| Cards in the composer | 4 appshots, and a second capture of one window replaces the first | `MAX_PENDING_APPSHOTS` |
| Old text | The newest 2 appshots keep their text. Older ones, when over 1,500 characters, shrink to their header and a note, but only under context pressure, because the rewrite breaks the prompt cache. | `prune_old_appshot_text` |

The numbers are starting points. Use `scripts/appshot_harness.py` on the windows you use every day to see the time and size of each read.

## Safety and privacy

- Windows of password managers (1Password, Bitwarden, KeePassXC, LastPass, Dashlane, Keychain Access, Passwords) are refused, picture and text.
- Secure text fields are never read.
- Freyja's own windows are never captured.
- The log has one line per capture with the source, the bundle id, the status and time of each part, and the character count. It holds no screen content.
- Nothing is stored apart from the message you send. The card is not saved if you remove it.

## Known gaps

- Some apps show no text through accessibility. Terminals that draw their own text (cmux), games and canvas apps give the picture only, and the card says so.
- Google Docs and similar web editors draw text on a canvas. A later step can read page text from the browser, as the jev DOM surface already does.
- The default key is a normal accelerator. OpenAI's both-Command-keys chord needs a key monitor with the Input Monitoring permission, it starts macOS dictation, and the ChatGPT app already holds it on this Mac.
- A node verb for the Anywhere plan (a brain on another machine asks this Mac for its frontmost window) is not built. It needs a consent step each time.
- The card does not yet show which model provider will receive the capture.

## Testing

```
uv run --extra dev pytest tests/test_appshot.py tests/test_voice_computer.py
npx tsx test-appshot.mjs
python scripts/appshot_harness.py --windows
python scripts/appshot_harness.py --app Finder --lines 30
```

The Python tests use a fake `freyja_native`, so they never touch the screen. The harness captures an open window with the same code the hotkey uses.

## Background: OpenAI Appshots

OpenAI describes an appshot as the visible text and the text that the app makes available outside the visible scroll area, plus an image of the window, attached to a chat when you press both Command keys (Windows: both Alt keys). The capture joins the current chat when you used one in the last 60 seconds, and otherwise starts a new chat. Administrators can switch it off with `allow_appshots = false` in `requirements.toml`.

Public bug reports in `openai/codex` shaped the design here: a capture that stops silently when a dependent step fails (52344), no way to cancel a stuck capture (33914), text that arrives while the image fails (51035), no way to start a capture except a physical key chord (50967), and a chord that also starts macOS dictation (51914).

Sources:

- https://learn.chatgpt.com/docs/appshots
- https://learn.chatgpt.com/docs/enterprise/managed-configuration.md
- https://9to5mac.com/2026/05/21/codex-for-mac-updated-with-new-appshots-feature-that-instantly-gives-chat-context/
- https://github.com/openai/codex/issues/52344
- https://manual.raycast.com/ai/screen-awareness.md
