# Adding (or changing) an LLM model — codepoint checklist

Freyja has model metadata scattered across **22 codepoints** in 11 files.
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

### 7a. `engine/anthropic_provider.py` — capability sets (Anthropic only)
Three more allowlists in the same file. They are *not* optional polish —
each one is a request the API rejects if the set is wrong:

- `INLINE_SYSTEM_MESSAGE_MODELS` — models that accept `role: "system"`
  mid-`messages`. Unsupported models 400 with `role 'system' is not
  supported on this model`, so `_convert_messages` squashes to a
  `[System context]:` user prefix instead. A model that *does* support
  it but is missing here silently loses the cached-prefix benefit.
- `FORCED_TOOL_CHOICE_UNSUPPORTED_MODELS` — models that 400 on
  `tool_choice` `{"type":"any"}` / `{"type":"tool"}`. Substring-matched,
  so a `-fast` suffix is tolerated. `complete_structured` falls back to
  `auto` for these. Missing entry on a rejecting model → every
  structured-output call 400s until the retry path catches it.
- `REFUSAL_FALLBACK_MODELS` — models whose safety classifiers can end a
  turn with `stop_reason="refusal"`. Listing a model opts it into
  server-side fallbacks to `REFUSAL_FALLBACK_TARGET`. Missing entry →
  a declined request is a dead turn instead of a rescued one.
- `ALWAYS_THINKING_MODELS` — models where thinking cannot be turned off
  by any means. Read by `supports_thinking_off`. Their reasoning ladders
  in #11 and #13 must omit the `none` rung.
- `BETWEEN_TOOLS_THINKING_MODELS` — models whose thinking-off switch is
  `thinking: {"type": "between_tools"}`. See #9a, which is the copy that
  actually shapes the request.

### 8. `engine/openai_provider.py` — `MODEL_CONTEXT_WINDOWS`
OpenAI-specific duplicate of #1. Read by the OpenAI provider's
constructor; falls back to `400_000` (not the 200k from constants.py).

Missing entry → smaller window for OpenAI models.

### 8a. `engine/openai_provider.py` — capability sets (OpenAI only)
- `REASONING_MODELS` — gates whether the `reasoning` parameter is sent
  at all. Missing entry → the model silently runs without reasoning and
  the effort selector in the UI does nothing.
- `NATIVE_COMPUTER_MODELS` — gates the native `computer_use` tool.
  Missing entry → computer-use sessions fall back to the generic path.

Note the effort ladder itself is **not** enforced here: the OpenAI
provider forwards whatever effort it is handed. `MODEL_REASONING_META`
(#11) is the only thing that stops an unsupported rung — e.g. `none` or
`minimal` on `gpt-6-astra` — from reaching the wire as a 400.

### 9. `engine/types.py` — `_ADAPTIVE_THINKING_MODEL_IDS`
Duplicate of #5. The engine types module needs it for type-level
inference without importing the provider (circular import). Comment in
file already says "keep in sync with anthropic_provider".

Missing entry → adaptive-thinking type guard misses the new model.

### 9a. `engine/types.py` — `_BETWEEN_TOOLS_THINKING_MODEL_IDS`
Models where "thinking off" is an explicit `thinking: {"type":
"between_tools"}` block rather than the absence of a `thinking` field.
Claude Sonnet 5.5 is the first. `ThinkingConfig.to_api_param` reads this
set, and `AnthropicProvider._build_request` is deliberately **not** gated
on `think_config.enabled` so the block can be emitted while thinking is
"off".

Three constraints the API enforces with a 400, all verified by live probe
on 2026-09-29:

| Request | Result |
|---|---|
| `{"type": "between_tools"}` alone | 200 |
| `between_tools` + `output_config.effort` of `xhigh`/`max` | 400 |
| `between_tools` + `display` (or `budget_tokens`, `block_binding`) | 400 |
| `{"type": "disabled"}` | 400, message points at `between_tools` |

`get_output_config` returns `None` whenever thinking is off, which is what
keeps rows 2 and 3 unreachable. Don't "helpfully" add a `display` key for
symmetry with the adaptive path.

Missing entry on a between_tools model → asking for `none` sends no
`thinking` field, which on such a model means **full adaptive thinking**.
It does not error; it just silently bills reasoning on the cheap fan-out
sub-agents that asked for none.

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

When adding model `claude-opus-X-Y`, grep for an existing model id you're
following (e.g. `claude-opus-4-8`) and ensure your new id appears in every
hit:

```sh
grep -rn "claude-opus-4-8" \
  engine/ bridge/ src/renderer/state/store.ts \
  src/renderer/components/ModelPicker.tsx
```

You should see roughly 20 hits (a model with a `-fast` tier roughly
doubles that — Opus 5.5 lands 24). The new id should land in the same
positions. Some hits are defaults and docstrings rather than registries
(`bridge/gateway/config.py`, `bridge/knowledge/learning/constants.py`,
`bridge/gateway/run.py` help text) — changing those is a separate
decision from registering the model, and adding a model does **not**
require changing them.

### Verify the provider still serves the models you already list

A model can vanish under you. On 2026-09-26 Fireworks removed nine models
from serverless inference; on 2026-09-29 a probe found that **9 of the 14
Fireworks ids in this repo returned 404** `Model not found, inaccessible,
and/or not deployed`, including `kimi-k2.6`, which was at the time the
single most-used fallback target in `FALLBACK_CHAINS`. Nothing failed
loudly, because a fallback target is only exercised when the primary is
already failing — the outage was latent.

Two lessons:

1. **Don't trust a provider changelog over the provider's API.** The
   Fireworks changelog listed `qwen3.7-plus` as still available and
   `glm-5.1` as never deprecated; both 404 in practice.
2. **Probe before you wire a fallback.** Listing a model in
   `GET /v1/models` is not the same as it being served — several dead ids
   were still in the list. The cheap check that actually answers it:

```sh
# Fireworks: is this id really served?
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST https://api.fireworks.ai/inference/v1/chat/completions \
  -H "Authorization: Bearer $FIREWORKS_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"accounts/fireworks/models/<id>","max_tokens":1,
       "messages":[{"role":"user","content":"hi"}]}'
```

Retired ids are kept as `FALLBACK_CHAINS` **keys** (so a session still
pinned to one degrades to something live) but must never appear as a
fallback **target**. `tests/test_model_registry_consistency.py` enforces
that, along with every registry model having a price and a chain.

### A model whose capabilities shrank

The checklist assumes a new model is a superset of the old one. Recent
models are not. Claude Opus 5.5 (Sep 2026) *removed* the ability to
disable thinking and *removed* forced tool use; Claude Sonnet 5.5
*replaced* `disabled` with `between_tools` (#9a) and also dropped forced
tool use; GPT-6 Astra and GPT-6.1 Sol dropped the `none` and `minimal`
effort rungs the GPT-5.x family had, while GPT-6 Luna kept `none`. Two
models in the same family released days apart can differ on this — check
each one rather than copying its sibling's ladder. For these,
the thing that keeps a bad request off the wire is **leaving a rung out
of `reasoningLevels`** in #11 and #13, because
`_normalize_reasoning_level` clamps an unlisted level back to the
model's default. Adding the rung "for consistency" with its sibling
models re-introduces the 400.

## Adding a whole new PROVIDER (not just a model)

A new provider (e.g. `engine/zai_provider.py`, added 2026-08 for GLM-5.3)
touches everything above **plus** these provider-level codepoints:

1. `engine/<name>_provider.py` — the provider class. Copy the closest
   OpenAI-compatible sibling (fireworks/cerebras) and adjust base URL,
   env var, and reasoning parameter shape.
2. `engine/providers.py` — `_create_single_provider` needs an
   `elif provider_name == "<name>"` branch, and every MODEL_REGISTRY
   entry uses that provider string.
3. `bridge/tools/agent_types.py` — `_env_var_for_model` maps model-id
   prefixes to env vars for sub-agent availability checks. A new
   provider's models must match BEFORE any overlapping prefix rule
   (glm-5.3 → ZAI_API_KEY had to precede the `glm*` → FIREWORKS rule).
4. `bridge/freyja_bridge.py` — `build_provider` is the BRIDGE's own
   family-dispatch factory (separate from the engine's
   `_create_single_provider`!). A family advertised in AVAILABLE_MODELS
   with no branch here raises "Unknown model family" the moment the
   user sends a message — this exact miss shipped with GLM-5.3.
   `tests/test_build_provider_coverage.py` now guards it: add the new
   family's env var to its `_FAKE_KEYS`.
5. `src/main/gatewayBridge.ts` — `LLM_PROVIDER_KEYS` is the allowlist
   of env vars propagated to the launchd gateway daemon's
   `~/.freyja/.env`. Missing entry → model works in the desktop app but
   the daemon replies "KEY is not set" to Slack messages.
5. `.env.example` and the README provider-keys table.

## Why no single registry?

Three boundaries (engine ↔ bridge ↔ renderer) and three failure modes
(provider-specific shape, runtime lookup without IPC, cold-start
fallback before bridge IPC). Each codepoint serves a different
boundary's needs. A consolidated registry would either need to be the
bridge's IPC catalog (which means the engine and renderer can't read
it synchronously) or generated code (which adds a codegen step).
For now: cross-reference comments at each codepoint + this checklist.

If the count grows past ~20, revisit the codegen question.
