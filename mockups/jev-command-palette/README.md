# ⌘K generative palette — standalone prototype

The first pass at the Jev-routed command palette, kept as a reference. It is a
self-contained page (`index.html`) plus a Python proxy (`server.py`, port 9500)
that calls TypeSafe's System One and builds widget specs with sample data.

The shipped version lives in the app, not here:

- `src/main/jevRouter.ts` — the Jev call (domain / sub-target / readiness), behind
  the `jev:classify` IPC channel.
- `src/renderer/lib/paletteIntent.ts` — slot parsing on every keystroke (who,
  when, time, PR, model, period, file type…) and widget choice.
- `src/renderer/components/JevGenerativeWidgets.tsx` — the cards, reading the
  real store (models, sessions, sub-agents, artifacts) and acting through it.
- `src/renderer/components/CommandPalette.tsx` — wiring, debounce, hysteresis.

Run the prototype with `python3 server.py` and open http://127.0.0.1:9500/.
