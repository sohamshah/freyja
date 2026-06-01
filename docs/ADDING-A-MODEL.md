# Adding (or changing) an LLM model — codepoint checklist

Freyja has model metadata scattered across **14 codepoints** in 11 files.
There is no single registry; the Python bridge, the engine providers,
and the TypeScript renderer each carry overlapping copies for reasons
(fallback when the bridge hasn't sent its catalog yet, runtime lookup
without an IPC round-trip, provider-specific capability flags).

**Every one of these must be updated together.** A missed entry doesn't
fail loudly — it silently falls back to a default that is almost always
wrong (200k context, "auto" reasoning, no thinking, missing from picker).
The Opus 4.8 incident that caused this doc: the bridge UI advertised 1M
context but the runtime capped at 200k because `engine/constants.py`
was missed. Compaction kicked in 5× earlier than the operator expected.

If you are reading this because you want to add or rename a model, work
through the checklist below in order.

---

## Python — engine + bridge

### 1. `engine/constants.py` — `MODEL_CONTEXT_WINDOWS`
Runtime context window. Read by:
- `engine/anthropic_provider.py` constructor (sets `self._context_window`,
  drives compaction decisions).
- `bridge/freyja_bridge.py` pressure-trigger code (`MODEL_CONTEXT_WINDOWS.get`).

Missing entry → falls back to `DEFAULT_CONTEXT_WINDOW = 200_000`.

### 2. `engine/providers.py` — `MODEL_REGISTRY`
Provider routing + context window + thinking flag. Read by:
- `get_provider_name(model)`, `get_context_window(model)`,
  `model_supports_thinking(model)`.
- The factory that builds a provider for a given model id.

Missing entry → `get_context_window` falls back to `200_000`,
`get_provider_name` raises.

### 3. `engine/providers.py` — `MODEL_PRICING_PER_M`
USD per 1M tokens, tuple `(input, output, cache_read[, cache_write])`.
Used by the cost meter (`session_spend` in the activity panel).

Missing entry → cost shows as `$0.000` forever; operator can't see real
spend.

### 4. `engine/providers.py` — `FALLBACK_CHAINS`
Ordered list of fallback model ids tried when the primary is
unavailable. Used by `resolve_model_choice` (sub-agent profiles) and
by the model picker's red-state recovery.

Missing entry → no fallback when the primary 503s. Not fatal but
degrades UX during provider hiccups.

### 5. `engine/anthropic_provider.py` — `ADAPTIVE_THINKING_MODELS`
Anthropic models that take `type="adaptive" + output_config.effort`
shape instead of the legacy `thinking={"type": "enabled", ...}` block.
Opus 4.7 and 4.8 are adaptive; Opus 4.6 and earlier are legacy.

Missing entry on a new adaptive model → request body is wrong, API
400s.

### 6. `engine/anthropic_provider.py` — `LEGACY_THINKING_MODELS`
Mirror of #5 for the pre-adaptive thinking shape. Union of the two
is `THINKING_MODELS`, which gates `supports_thinking`.

Missing entry → `supports_thinking` returns False, no `thinking` block
sent, the model runs without extended thinking even when asked.

### 7. `engine/anthropic_provider.py` — `FAST_MODE_MODELS`
Allowlist of models that accept `extra_body={"speed": "fast"}` and the
`anthropic-beta: fast-mode-...` header. If you're adding a `-fast`
variant, the base id (without `-fast`) goes here.

Missing entry → `_fast_mode` request raises ValueError at construct
time before any API call.

### 8. `engine/openai_provider.py` — `MODEL_CONTEXT_WINDOWS`
OpenAI-specific duplicate of #1. Read by the OpenAI provider's
constructor; falls back to `400_000` (not the 200k from constants.py).

Missing entry → smaller window for OpenAI models.

### 9. `engine/types.py` — `_ADAPTIVE_THINKING_MODEL_IDS`
Duplicate of #5. The engine types module needs it for type-level
inference without importing the provider (circular import). Comment in
file already says "keep in sync with anthropic_provider".

Missing entry → adaptive-thinking type guard misses the new model.

### 10. `bridge/freyja_bridge.py` — `AVAILABLE_MODELS`
The catalog the bridge sends to the renderer on the `ready` event.
Drives the model picker, the session header label, and the reasoning
selector. Each entry carries: `id, family, label, tier, contextWindow,
thinking, envVar, description`.

Missing entry → model is invisible in the picker even if the bridge can
run it. (FALLBACK_MODELS in the renderer is the only thing keeping it
selectable at all.)

### 11. `bridge/freyja_bridge.py` — `MODEL_REASONING_META`
Per-model reasoning capability: `reasoningMode` (effort / adaptive /
budget / required / none), `reasoningLevels` list, `reasoningDefault`.
Read into the AVAILABLE_MODELS payload at send-time.

Missing entry → reasoning picker shows no options or the default is
wrong. Set this even if reasoningMode is "none".

---

## TypeScript — renderer

### 12. `src/renderer/state/store.ts` — `MODEL_CONTEXT_WINDOWS`
Renderer-side fallback for `usage.contextWindow` before the bridge has
sent its first usage event. Used by `contextWindowFor(model)`. The
`REQUEST CONTEXT 32k/N` display in the activity panel reads this until
a real usage event lands.

Missing entry → falls back to `200_000`, panel shows wrong denominator
during the first turn of a new session.

### 13. `src/renderer/state/store.ts` — `MODEL_REASONING_FALLBACKS`
Renderer-side fallback for `reasoningLevels` / `reasoningDefault` when
the bridge hasn't sent capability metadata yet.

Missing entry → reasoning UI shows generic options or no selector at
all on cold start.

### 14. `src/renderer/components/ModelPicker.tsx` — `FALLBACK_MODELS`
Hardcoded model list used by the picker when the bridge's
`AVAILABLE_MODELS` catalog hasn't loaded yet (first paint, gateway
disconnected, etc.). Same shape as `AVAILABLE_MODELS`.

Missing entry → model is unselectable from the picker on cold start.

---

## Quick-reference grep checklist

When adding model `claude-opus-4-X`, grep for an existing model id you're
following (e.g. `claude-opus-4-7`) and ensure your new id appears in every
hit:

```sh
grep -rn "claude-opus-4-7" \
  engine/ bridge/ src/renderer/state/store.ts \
  src/renderer/components/ModelPicker.tsx
```

You should see roughly 14 hits. The new id should land in the same
positions.

## Why no single registry?

Three boundaries (engine ↔ bridge ↔ renderer) and three failure modes
(provider-specific shape, runtime lookup without IPC, cold-start
fallback before bridge IPC). Each codepoint serves a different
boundary's needs. A consolidated registry would either need to be the
bridge's IPC catalog (which means the engine and renderer can't read
it synchronously) or generated code (which adds a codegen step).
For now: cross-reference comments at each codepoint + this checklist.

If the count grows past ~20, revisit the codegen question.
