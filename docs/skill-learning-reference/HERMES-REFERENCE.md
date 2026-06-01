# Hermes Skill Learning Reference (for Freyja port)

## 1. Load-bearing prompts (verbatim)

### 1.1 SKILL_REVIEW_PROMPT

Source: `/Users/sohamshah/work/services/hermes-agent/agent/background_review.py`

```python
_SKILL_REVIEW_PROMPT = (
    "Review the conversation above and update the skill library. Be "
    "ACTIVE — most sessions produce at least one skill update, even if "
    "small. A pass that does nothing is a missed learning opportunity, "
    "not a neutral outcome.\n\n"
    "Target shape of the library: CLASS-LEVEL skills, each with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries. This "
    "shapes HOW you update, not WHETHER you update.\n\n"
    "Signals to look for (any one of these warrants action):\n"
    "  • User corrected your style, tone, format, legibility, or "
    "verbosity. Frustration signals like 'stop doing X', 'this is too "
    "verbose', 'don't format like this', 'why are you explaining', "
    "'just give me the answer', 'you always do Y and I hate it', or an "
    "explicit 'remember this' are FIRST-CLASS skill signals, not just "
    "memory signals. Update the relevant skill(s) to embed the "
    "preference so the next session starts already knowing.\n"
    "  • User corrected your workflow, approach, or sequence of steps. "
    "Encode the correction as a pitfall or explicit step in the skill "
    "that governs that class of task.\n"
    "  • Non-trivial technique, fix, workaround, debugging path, or "
    "tool-usage pattern emerged that a future session would benefit "
    "from. Capture it.\n"
    "  • A skill that got loaded or consulted this session turned out "
    "to be wrong, missing a step, or outdated. Patch it NOW.\n\n"
    "Preference order — prefer the earliest action that fits, but do "
    "pick one when a signal above fired:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Look back through the "
    "conversation for skills the user loaded via /skill-name or you "
    "read via skill_view. If any of them covers the territory of the "
    "new learning, PATCH that one first. It is the skill that was in "
    "play, so it's the right one to extend.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (via skills_list + skill_view). "
    "If no loaded skill fits but an existing class-level skill does, "
    "patch it. Add a subsection, a pitfall, or broaden a trigger.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella. Skills can be "
    "packaged with three kinds of support files — use the right "
    "directory per kind:\n"
    "     • `references/<topic>.md` — session-specific detail (error "
    "transcripts, reproduction recipes, provider quirks) AND "
    "condensed knowledge banks: quoted research, API docs, external "
    "authoritative excerpts, or domain notes you found while working "
    "on the problem. Write it concise and for the value of the task, "
    "not as a full mirror of upstream docs.\n"
    "     • `templates/<name>.<ext>` — starter files meant to be "
    "copied and modified (boilerplate configs, scaffolding, a "
    "known-good example the agent can `reproduce with modifications`).\n"
    "     • `scripts/<name>.<ext>` — statically re-runnable actions "
    "the skill can invoke directly (verification scripts, fixture "
    "generators, deterministic probes, anything the agent should run "
    "rather than hand-type each time).\n"
    "     Add support files via skill_manage action=write_file with "
    "file_path starting 'references/', 'templates/', or 'scripts/'. "
    "The umbrella's SKILL.md should gain a one-line pointer to any "
    "new support file so future agents know it exists.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing "
    "skill covers the class. The name MUST be at the class level. "
    "The name MUST NOT be a specific PR number, error string, feature "
    "codename, library-alone name, or 'fix-X / debug-Y / audit-Z-today' "
    "session artifact. If the proposed name only makes sense for "
    "today's task, it's wrong — fall back to (1), (2), or (3).\n\n"
    "User-preference embedding (important): when the user expressed a "
    "style/format/workflow preference, the update belongs in the "
    "SKILL.md body, not just in memory. Memory captures 'who the user "
    "is and what the current situation and state of your operations "
    "are'; skills capture 'how to do this class of task for this "
    "user'. When they complain about how you handled a task, the "
    "skill that governs that task needs to carry the lesson.\n\n"
    "If you notice two existing skills that overlap, note it in your "
    "reply — the background curator handles consolidation at scale.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • Bundled skills (shipped with Hermes, e.g. 'hermes-agent').\n"
    "  • Hub-installed skills (installed via 'hermes skills install').\n"
    "Pinned skills (marked via 'hermes curator pin') CAN be improved — "
    "pin only blocks deletion/archive/consolidation by the curator, not "
    "content updates. Patch them when a pitfall or missing step turns up, "
    "same as any other agent-created skill.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture (these become persistent self-imposed constraints "
    "that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "'Nothing to save.' is a real option but should NOT be the "
    "default. If the session ran smoothly with no corrections and "
    "produced no new technique, just say 'Nothing to save.' and stop. "
    "Otherwise, act."
)
```

### 1.2 MEMORY_REVIEW_PROMPT

Source: `/Users/sohamshah/work/services/hermes-agent/agent/background_review.py`

```python
_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)
```

### 1.3 COMBINED_REVIEW_PROMPT

Source: `/Users/sohamshah/work/services/hermes-agent/agent/background_review.py`

```python
_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "desires, preferences, personal details, or expectations about "
    "how you should behave? Save facts about the user and durable "
    "preferences with the memory tool.\n\n"
    "**Skills**: how to do this class of task. Be ACTIVE — most "
    "sessions produce at least one skill update. A pass that does "
    "nothing is a missed learning opportunity, not a neutral outcome.\n\n"
    "Target shape of the skill library: CLASS-LEVEL skills with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries.\n\n"
    "Signals that warrant a skill update (any one is enough):\n"
    "  • User corrected your style, tone, format, legibility, "
    "verbosity, or approach. Frustration is a FIRST-CLASS skill "
    "signal, not just a memory signal. 'stop doing X', 'don't format "
    "like this', 'I hate when you Y' — embed the lesson in the skill "
    "that governs that task so the next session starts fixed.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path "
    "emerged.\n"
    "  • A skill that was loaded or consulted turned out wrong, "
    "missing, or outdated — patch it now.\n\n"
    "Preference order for skills — pick the earliest that fits:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were "
    "loaded via /skill-name or skill_view in the conversation. If one "
    "of them covers the learning, PATCH it first. It was in play; "
    "it's the right place.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (skills_list + skill_view to "
    "find the right one). Patch it.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella via "
    "skill_manage action=write_file. Three kinds: "
    "`references/<topic>.md` for session-specific detail OR condensed "
    "knowledge banks (quoted research, API docs excerpts, domain "
    "notes) written concise and task-focused; `templates/<name>.<ext>` "
    "for starter files meant to be copied and modified; "
    "`scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification, fixture generators, probes). Add a one-line "
    "pointer in SKILL.md so future agents find them.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. "
    "Name at the class level — NOT a PR number, error string, "
    "codename, library-alone name, or 'fix-X / debug-Y' session "
    "artifact. If the name only fits today's task, fall back to (1), "
    "(2), or (3).\n\n"
    "User-preference embedding: when the user complains about how "
    "you handled a task, update the skill that governs that task — "
    "memory alone isn't enough. Memory says 'who the user is and "
    "what the current situation and state of your operations are'; "
    "skills say 'how to do this class of task for this user'. Both "
    "should carry user-preference lessons when relevant.\n\n"
    "If you notice overlapping existing skills, mention it — the "
    "background curator handles consolidation.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • Bundled skills (shipped with Hermes, e.g. 'hermes-agent').\n"
    "  • Hub-installed skills (installed via 'hermes skills install').\n"
    "Pinned skills (marked via 'hermes curator pin') CAN be improved — "
    "pin only blocks deletion/archive/consolidation by the curator, not "
    "content updates. Patch them when a pitfall or missing step turns up, "
    "same as any other agent-created skill.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture as skills (these become persistent self-imposed "
    "constraints that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "Act on whichever of the two dimensions has real signal. If "
    "genuinely nothing stands out on either, say 'Nothing to save.' "
    "and stop — but don't reach for that conclusion as a default."
)
```

### 1.4 MEMORY_GUIDANCE

Source: `/Users/sohamshah/work/services/hermes-agent/agent/prompt_builder.py`

```python
MEMORY_GUIDANCE = (
    "You have persistent memory across sessions. Save durable facts using the memory "
    "tool: user preferences, environment details, tool quirks, and stable conventions. "
    "Memory is injected into every turn, so keep it compact and focused on facts that "
    "will still matter later.\n"
    "Prioritize what reduces future user steering — the most valuable memory is one "
    "that prevents the user from having to correct or remind you again. "
    "User preferences and recurring corrections matter more than procedural task details.\n"
    "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
    "state to memory; use session_search to recall those from past transcripts. "
    "Specifically: do not record PR numbers, issue numbers, commit SHAs, 'fixed bug X', "
    "'submitted PR Y', 'Phase N done', file counts, or any artifact that will be stale "
    "in 7 days. If a fact will be stale in a week, it does not belong in memory. "
    "If you've discovered a new way to do something, solved a problem that could be "
    "necessary later, save it as a skill with the skill tool.\n"
    "Write memories as declarative facts, not instructions to yourself. "
    "'User prefers concise responses' ✓ — 'Always respond concisely' ✗. "
    "'Project uses pytest with xdist' ✓ — 'Run tests with pytest -n 4' ✗. "
    "Imperative phrasing gets re-read as a directive in later sessions and can "
    "cause repeated work or override the user's current request. Procedures and "
    "workflows belong in skills, not memory."
)
```

### 1.5 SKILLS_GUIDANCE

Source: `/Users/sohamshah/work/services/hermes-agent/agent/prompt_builder.py`

```python
SKILLS_GUIDANCE = (
    "After completing a complex task (5+ tool calls), fixing a tricky error, "
    "or discovering a non-trivial workflow, save the approach as a "
    "skill with skill_manage so you can reuse it next time.\n"
    "When using a skill and finding it outdated, incomplete, or wrong, "
    "patch it immediately with skill_manage(action='patch') — don't wait to be asked. "
    "Skills that aren't maintained become liabilities."
)
```

### 1.6 The "Do NOT capture" denylist (extracted from SKILL_REVIEW_PROMPT)

This block is duplicated in both `_SKILL_REVIEW_PROMPT` and `_COMBINED_REVIEW_PROMPT` — it is the single most load-bearing defense against the agent hardening transient failures into permanent self-imposed refusals. Extracted verbatim:

```text
Do NOT capture (these become persistent self-imposed constraints
that bite you later when the environment changes):
  • Environment-dependent failures: missing binaries, fresh-install
errors, post-migration path mismatches, 'command not found',
unconfigured credentials, uninstalled packages. The user can fix
these — they are not durable rules.
  • Negative claims about tools or features ('browser tools do not
work', 'X tool is broken', 'cannot use Y from execute_code'). These
harden into refusals the agent cites against itself for months
after the actual problem was fixed.
  • Session-specific transient errors that resolved before the
conversation ended. If retrying worked, the lesson is the retry
pattern, not the original failure.
  • One-off task narratives. A user asking 'summarize today's
market' or 'analyze this PR' is not a class of work that warrants
a skill.

If a tool failed because of setup state, capture the FIX (install
command, config step, env var to set) under an existing setup or
troubleshooting skill — never 'this tool does not work' as a
standalone constraint.
```

---

## 2. Fork construction (verbatim code)

### 2.1 Daemon thread spawn pattern

The full `_run_review_in_thread` function (the worker executed in the background-review daemon thread, spans Hermes lines 327–559) — this is the single canonical place that combines spawn + redirect + whitelist + summary + cleanup. Source: `/Users/sohamshah/work/services/hermes-agent/agent/background_review.py`.

```python
def _run_review_in_thread(
    agent: Any,
    messages_snapshot: List[Dict],
    prompt: str,
) -> None:
    """Worker function executed in the background-review daemon thread.

    Spawns a forked ``AIAgent`` inheriting the parent's runtime, runs the
    review prompt, and surfaces a compact action summary back to the user
    via ``agent._safe_print`` and ``agent.background_review_callback``.
    """
    # Local import to avoid a hard circular dep at module load.
    from run_agent import AIAgent
    from tools.terminal_tool import set_approval_callback as _set_approval_callback

    # Install a non-interactive approval callback on this worker
    # thread so any dangerous-command guard the review agent trips
    # resolves to "deny" instead of falling back to input() -- which
    # deadlocks against the parent's prompt_toolkit TUI (#15216).
    # Same pattern as _subagent_auto_deny in tools/delegate_tool.py.
    def _bg_review_auto_deny(command, description, **kwargs):
        logger.warning(
            "Background review auto-denied dangerous command: %s (%s)",
            command, description,
        )
        return "deny"
    try:
        _set_approval_callback(_bg_review_auto_deny)
    except Exception:
        pass

    review_agent = None
    review_messages: List[Dict] = []
    try:
        with open(os.devnull, "w", encoding="utf-8") as _devnull, \
             contextlib.redirect_stdout(_devnull), \
             contextlib.redirect_stderr(_devnull):
            # Inherit the parent agent's live runtime (provider, model,
            # base_url, api_key, api_mode) so the fork uses the exact
            # same credentials the main turn is using.  Without this,
            # AIAgent.__init__ re-runs auto-resolution from env vars,
            # which fails for OAuth-only providers, session-scoped
            # creds, or credential-pool setups where the resolver can't
            # reconstruct auth from scratch -- producing the spurious
            # "No LLM provider configured" warning at end of turn.
            _parent_runtime = agent._current_main_runtime()
            _parent_api_mode = _parent_runtime.get("api_mode") or None
            # The review fork needs to call agent-loop tools (memory,
            # skill_manage). Those tools require Hermes' own dispatch,
            # which the codex_app_server runtime bypasses entirely
            # (it runs the turn inside codex's subprocess). So when
            # the parent is on codex_app_server, downgrade the review
            # fork to codex_responses — same auth/credentials, but
            # talks to the OpenAI Responses API directly so Hermes
            # owns the loop and the agent-loop tools dispatch.
            if _parent_api_mode == "codex_app_server":
                _parent_api_mode = "codex_responses"
            # skip_memory=True keeps the review fork from
            # touching external memory plugins (honcho, mem0,
            # supermemory, etc.).  Without it, the fork's
            # __init__ rebuilds its own _memory_manager from
            # config, scoped to the parent's session_id, and
            # run_conversation() then leaks the harness prompt
            # into the user's real memory namespace via three
            # ingestion sites: on_turn_start (cadence + turn
            # message), prefetch_all (recall query), and
            # sync_all (harness prompt + review output recorded
            # as a (user, assistant) turn pair).  Built-in
            # MEMORY.md / USER.md state is re-bound from the
            # parent below so memory(action="add") writes from
            # the review still land on disk; the review just
            # has zero side effects on external providers.
            # Match parent's toolset config so ``tools[]`` is byte-identical
            # in the request body — Anthropic's cache key includes it.
            # (The runtime whitelist below still restricts dispatch.)
            review_agent = AIAgent(
                model=agent.model,
                max_iterations=16,
                quiet_mode=True,
                platform=agent.platform,
                provider=agent.provider,
                api_mode=_parent_api_mode,
                base_url=_parent_runtime.get("base_url") or None,
                api_key=_parent_runtime.get("api_key") or None,
                credential_pool=getattr(agent, "_credential_pool", None),
                parent_session_id=agent.session_id,
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                skip_memory=True,
            )
            review_agent._memory_write_origin = "background_review"
            review_agent._memory_write_context = "background_review"
            review_agent._memory_store = agent._memory_store
            review_agent._memory_enabled = agent._memory_enabled
            review_agent._user_profile_enabled = agent._user_profile_enabled
            review_agent._memory_nudge_interval = 0
            review_agent._skill_nudge_interval = 0
            # Suppress all status/warning emits from the fork so the
            # user only sees the final successful-action summary.
            # Without this, mid-review "Iteration budget exhausted",
            # rate-limit retries, compression warnings, and other
            # lifecycle messages bubble up through _emit_status ->
            # _vprint and leak past the stdout redirect (they go via
            # _print_fn/status_callback, which bypass sys.stdout).
            review_agent.suppress_status_output = True
            # Inherit the parent's cached system prompt verbatim so
            # the review fork's outbound HTTP request hits the same
            # Anthropic/OpenRouter prefix cache the parent warmed.
            # Without this, the fork rebuilds the system prompt from
            # scratch (fresh _hermes_now() timestamp, fresh
            # session_id, narrower toolset → different skills_prompt)
            # and the byte-exact prefix-cache key misses. See
            # issue #25322 and PR #17276 for the full analysis +
            # measured impact (~26% end-to-end cost reduction on
            # Sonnet 4.5).
            review_agent._cached_system_prompt = agent._cached_system_prompt
            # Defensive: pin session_start + session_id to the
            # parent's so any code path that re-renders parts of
            # the system prompt (compression, plugin hooks) still
            # produces byte-identical output. The cached-prompt
            # assignment above already short-circuits the normal
            # rebuild path, but these pins guarantee parity even
            # if a future code path bypasses the cache.
            review_agent.session_start = agent.session_start
            review_agent.session_id = agent.session_id

            from model_tools import get_tool_definitions
            from hermes_cli.plugins import (
                set_thread_tool_whitelist,
                clear_thread_tool_whitelist,
            )

            review_whitelist = {
                t["function"]["name"]
                for t in get_tool_definitions(
                    enabled_toolsets=["memory", "skills"],
                    quiet_mode=True,
                )
            }
            set_thread_tool_whitelist(
                review_whitelist,
                deny_msg_fmt=(
                    "Background review denied non-whitelisted tool: "
                    "{tool_name}. Only memory/skill tools are allowed."
                ),
            )
            try:
                review_agent.run_conversation(
                    user_message=(
                        prompt
                        + "\n\nYou can only call memory and skill "
                        "management tools. Other tools will be denied "
                        "at runtime — do not attempt them."
                    ),
                    conversation_history=messages_snapshot,
                )
            finally:
                clear_thread_tool_whitelist()

            # Snapshot review actions before teardown. close() is allowed to
            # clean per-session state, but the user-visible self-improvement
            # summary still needs the completed review agent's tool results.
            review_messages = list(getattr(review_agent, "_session_messages", []))

            # Tear down memory providers while stdout is still
            # redirected so background thread teardown (Honcho flush,
            # Hindsight sync, etc.) stays silent.  The finally block
            # below is a safety net for the exception path.
            try:
                review_agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                review_agent.close()
            except Exception:
                pass
            review_agent = None

        # Scan the review agent's messages for successful tool actions
        # and surface a compact summary to the user. Tool messages
        # already present in messages_snapshot must be skipped, since
        # the review agent inherits that history and would otherwise
        # re-surface stale "created"/"updated" messages from the prior
        # conversation as if they just happened (issue #14944).
        actions = summarize_background_review_actions(
            review_messages,
            messages_snapshot,
        )

        if actions:
            summary = " · ".join(dict.fromkeys(actions))
            agent._safe_print(
                f"  💾 Self-improvement review: {summary}"
            )
            _bg_cb = agent.background_review_callback
            if _bg_cb:
                try:
                    _bg_cb(
                        f"💾 Self-improvement review: {summary}"
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.warning("Background memory/skill review failed: %s", e)
        agent._emit_auxiliary_failure("background review", e)
    finally:
        # Safety-net cleanup for the exception path.  Normal
        # completion already shut down inside redirect_stdout above.
        # Re-open devnull here so any teardown output (Honcho flush,
        # Hindsight sync, background thread joins) stays silent even
        # on the exception path where redirect_stdout already exited.
        if review_agent is not None:
            try:
                with open(os.devnull, "w", encoding="utf-8") as _fn, \
                     contextlib.redirect_stdout(_fn), \
                     contextlib.redirect_stderr(_fn):
                    try:
                        review_agent.shutdown_memory_provider()
                    except Exception:
                        pass
                    try:
                        review_agent.close()
                    except Exception:
                        pass
            except Exception:
                pass
        # Clear the approval callback on this bg-review thread so a
        # recycled thread-id doesn't inherit a stale reference.
        try:
            _set_approval_callback(None)
        except Exception:
            pass
```

### 2.2 Forked agent constructor (which attributes inherited, why)

The constructor + inherited-attribute block, isolated. Every assignment defends against a specific failure mode — these notes are load-bearing and must survive the port.

```python
review_agent = AIAgent(
    model=agent.model,
    max_iterations=16,
    quiet_mode=True,
    platform=agent.platform,
    provider=agent.provider,
    api_mode=_parent_api_mode,
    base_url=_parent_runtime.get("base_url") or None,
    api_key=_parent_runtime.get("api_key") or None,
    credential_pool=getattr(agent, "_credential_pool", None),
    parent_session_id=agent.session_id,
    enabled_toolsets=getattr(agent, "enabled_toolsets", None),
    disabled_toolsets=getattr(agent, "disabled_toolsets", None),
    skip_memory=True,
)
review_agent._memory_write_origin = "background_review"
review_agent._memory_write_context = "background_review"
review_agent._memory_store = agent._memory_store
review_agent._memory_enabled = agent._memory_enabled
review_agent._user_profile_enabled = agent._user_profile_enabled
review_agent._memory_nudge_interval = 0
review_agent._skill_nudge_interval = 0
review_agent.suppress_status_output = True
review_agent._cached_system_prompt = agent._cached_system_prompt
review_agent.session_start = agent.session_start
review_agent.session_id = agent.session_id
```

Why each attribute matters (verbatim from artifact design notes):

1. `model / provider / api_mode / base_url / api_key / credential_pool` — without these, `AIAgent.__init__` re-runs env-var auto-resolution, which **fails** for OAuth-only providers, session-scoped creds, and credential-pool setups (produces "No LLM provider configured" warning).
2. `codex_app_server → codex_responses` downgrade — `codex_app_server` runs the turn inside codex's subprocess and bypasses Hermes' tool dispatch entirely, so `memory`/`skill_manage` wouldn't dispatch. Downgrade preserves auth but routes through OpenAI Responses API so Hermes owns the loop.
3. `enabled_toolsets / disabled_toolsets` — must match parent so the request body's `tools[]` array is **byte-identical** (Anthropic's cache key includes the tools array). The runtime whitelist restricts dispatch separately; this is purely for wire-format parity.
4. `skip_memory=True` — without it, the fork rebuilds its own `_memory_manager` scoped to parent's `session_id`, and `run_conversation()` leaks the harness prompt into the user's real memory namespace via **three** ingestion sites (`on_turn_start`, `prefetch_all`, `sync_all`). Built-in MEMORY.md/USER.md state is then re-bound manually below so `memory(action="add")` writes still land on disk — external providers (Honcho/mem0/supermemory) just see nothing.
5. `_memory_store / _memory_enabled / _user_profile_enabled` — re-bind built-in stores from parent so the fork CAN still write to MEMORY.md/USER.md even though `skip_memory=True` disabled external providers.
6. `_memory_write_origin / _memory_write_context = "background_review"` — provenance tagging so downstream telemetry distinguishes background-review writes from foreground writes.
7. `_memory_nudge_interval = 0 / _skill_nudge_interval = 0` — prevents the review fork from recursively spawning its own background review (infinite-recursion guard).
8. `suppress_status_output = True` — mid-review status messages ("Iteration budget exhausted", rate-limit retries, compression warnings) bubble through `_emit_status → _vprint` via `_print_fn/status_callback` which **bypass `sys.stdout`** and leak past the `redirect_stdout` block. This flag is the only way to silence them.
9. `_cached_system_prompt = agent._cached_system_prompt` — **the big cost lever**. Without it the fork rebuilds a fresh system prompt (new timestamp, new session_id, narrower toolset → different skills_prompt) and the prefix-cache misses. PR #17276 / issue #25322 measured ~26% end-to-end cost reduction on Sonnet 4.5. MUST be set AFTER constructor (which would clear it) and BEFORE `run_conversation` (which would rebuild it).
10. `session_start / session_id` — defensive pins; if any code path (compression, plugin hooks) bypasses the cached-prompt short-circuit and re-renders, these guarantee byte-identical output.

### 2.3 Stdout/stderr redirection block

Two redirect blocks: the primary one wrapping the entire fork lifetime, and the safety-net one in the exception-path `finally`.

```python
with open(os.devnull, "w", encoding="utf-8") as _devnull, \
     contextlib.redirect_stdout(_devnull), \
     contextlib.redirect_stderr(_devnull):
    # ... entire fork construction, run_conversation, teardown ...
```

```python
# Safety-net cleanup for the exception path. Normal
# completion already shut down inside redirect_stdout above.
# Re-open devnull here so any teardown output (Honcho flush,
# Hindsight sync, background thread joins) stays silent even
# on the exception path where redirect_stdout already exited.
if review_agent is not None:
    try:
        with open(os.devnull, "w", encoding="utf-8") as _fn, \
             contextlib.redirect_stdout(_fn), \
             contextlib.redirect_stderr(_fn):
            try:
                review_agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                review_agent.close()
            except Exception:
                pass
    except Exception:
        pass
```

The redirect alone is **not sufficient** — it only catches code paths that write through `sys.stdout`/`sys.stderr`. Status emits that go through `_print_fn`/`status_callback` bypass this entirely; that is why `suppress_status_output = True` on the review agent is also required.

### 2.4 Tool whitelist enforcement

Source: `/Users/sohamshah/work/services/hermes-agent/agent/background_review.py`.

```python
from model_tools import get_tool_definitions
from hermes_cli.plugins import (
    set_thread_tool_whitelist,
    clear_thread_tool_whitelist,
)

review_whitelist = {
    t["function"]["name"]
    for t in get_tool_definitions(
        enabled_toolsets=["memory", "skills"],
        quiet_mode=True,
    )
}
set_thread_tool_whitelist(
    review_whitelist,
    deny_msg_fmt=(
        "Background review denied non-whitelisted tool: "
        "{tool_name}. Only memory/skill tools are allowed."
    ),
)
try:
    review_agent.run_conversation(
        user_message=(
            prompt
            + "\n\nYou can only call memory and skill "
            "management tools. Other tools will be denied "
            "at runtime — do not attempt them."
        ),
        conversation_history=messages_snapshot,
    )
finally:
    clear_thread_tool_whitelist()
```

Three defenses in one pattern: (a) `tools[]` in the request stays byte-identical to the parent (cache-key parity), (b) dispatch-time enforcement blocks any non-memory/non-skill call regardless of what the model emits, (c) the user-message addendum tells the model the enforcement is real so it doesn't waste iterations attempting bash/edit/browser. The `try/finally clear_thread_tool_whitelist()` is essential — thread-id recycling would otherwise leak the whitelist into unrelated threads.

### 2.5 Action summarizer

Source: `/Users/sohamshah/work/services/hermes-agent/agent/background_review.py`.

```python
def summarize_background_review_actions(
    review_messages: List[Dict],
    prior_snapshot: List[Dict],
) -> List[str]:
    """Build the human-facing action summary for a background review pass.

    Walks the review agent's session messages and collects "successful tool
    action" descriptions to surface to the user (e.g. "Memory updated").
    Tool messages already present in ``prior_snapshot`` are skipped so we
    don't re-surface stale results from the prior conversation that the
    review agent inherited via ``conversation_history`` (issue #14944).

    Matching is by ``tool_call_id`` when available, with a content-equality
    fallback for tool messages that lack one.
    """
    existing_tool_call_ids = set()
    existing_tool_contents = set()
    for prior in prior_snapshot or []:
        if not isinstance(prior, dict) or prior.get("role") != "tool":
            continue
        tcid = prior.get("tool_call_id")
        if tcid:
            existing_tool_call_ids.add(tcid)
        else:
            content = prior.get("content")
            if isinstance(content, str):
                existing_tool_contents.add(content)

    actions: List[str] = []
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id")
        if tcid and tcid in existing_tool_call_ids:
            continue
        if not tcid:
            content_str = msg.get("content")
            if isinstance(content_str, str) and content_str in existing_tool_contents:
                continue
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get("success"):
            continue
        message = data.get("message", "")
        target = data.get("target", "")
        if "created" in message.lower():
            actions.append(message)
        elif "updated" in message.lower():
            actions.append(message)
        elif "added" in message.lower() or (target and "add" in message.lower()):
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
        elif "Entry added" in message:
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
        elif "removed" in message.lower() or "replaced" in message.lower():
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
    return actions
```

---

## 3. Cadence counters (verbatim code)

### 3.1 Counter init

Source: `/Users/sohamshah/work/services/hermes-agent/agent/agent_init.py`.

```python
# Persistent memory (MEMORY.md + USER.md) -- loaded from disk
agent._memory_store = None
agent._memory_enabled = False
agent._user_profile_enabled = False
agent._memory_nudge_interval = 10
agent._turns_since_memory = 0
agent._iters_since_skill = 0
if not skip_memory:
    try:
        mem_config = _agent_cfg.get("memory", {})
        agent._memory_enabled = mem_config.get("memory_enabled", False)
        agent._user_profile_enabled = mem_config.get("user_profile_enabled", False)
        agent._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
        if agent._memory_enabled or agent._user_profile_enabled:
            from tools.memory_tool import MemoryStore
            agent._memory_store = MemoryStore(
                memory_char_limit=mem_config.get("memory_char_limit", 2200),
                user_char_limit=mem_config.get("user_char_limit", 1375),
            )
            agent._memory_store.load_from_disk()
    except Exception:
        pass  # Memory is optional -- don't break agent init



# Skills config: nudge interval for skill creation reminders
agent._skill_nudge_interval = 10
try:
    skills_config = _agent_cfg.get("skills", {})
    agent._skill_nudge_interval = int(skills_config.get("creation_nudge_interval", 10))
except Exception:
    pass
```

Asymmetry to preserve: `_turns_since_memory` and `_iters_since_skill` are **runtime counters** initialized to 0 and incremented per turn/iter; `_memory_nudge_interval` and `_skill_nudge_interval` are **config-driven thresholds** defaulting to 10 that the counters are compared against. Defaults MUST be set above the try/except so a malformed config still leaves all four fields present. `skip_memory` gates only the config-read branch — counters are set unconditionally because skill nudging runs independently of memory state. YAML key paths differ: memory uses `agent.memory.nudge_interval`, skills use `agent.skills.creation_nudge_interval`.

### 3.2 Counter increment + trip check

The artifacts as supplied don't include the increment/trip-check code from `background_review.py` (line ~580 spawn site referenced in design notes), but the contract implied by the counter init + spawn-site reference is:

- After each user turn: `agent._turns_since_memory += 1`; if `agent._turns_since_memory >= agent._memory_nudge_interval`, fire the memory-review trigger and reset to 0.
- After each agent iteration (tool-call loop iter): `agent._iters_since_skill += 1`; if `agent._iters_since_skill >= agent._skill_nudge_interval`, fire the skill-review trigger and reset to 0.
- When **only** memory fires → use `_MEMORY_REVIEW_PROMPT`. When **only** skill fires → use `_SKILL_REVIEW_PROMPT`. When **both** fire on the same turn → use `_COMBINED_REVIEW_PROMPT`.

The review fork sets both `_memory_nudge_interval = 0` and `_skill_nudge_interval = 0` on itself precisely to prevent recursion (see 2.2 attribute 7).

### 3.3 Spawn site

The artifacts don't include the literal `spawn_background_review_thread` function body (referenced as `background_review.py` line ~580). The contract from the design notes is: it picks one of the three prompts based on which triggers fired, snapshots `messages_snapshot = list(agent._session_messages)`, and starts a daemon `threading.Thread(target=_run_review_in_thread, args=(agent, messages_snapshot, prompt), daemon=True).start()`. The parent agent then continues; the review runs concurrently and emits its summary via `agent._safe_print` and `agent.background_review_callback` when done.

---

## 4. Skill management tool (verbatim)

### 4.1 Action dispatch table

Source: `/Users/sohamshah/work/services/hermes-agent/tools/skill_manager_tool.py`.

```python
def skill_manage(
    action: str,
    name: str,
    content: str = None,
    category: str = None,
    file_path: str = None,
    file_content: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    absorbed_into: str = None,
) -> str:
    """
    Manage user-created skills. Dispatches to the appropriate action handler.

    Returns JSON string with results.
    """
    if action == "create":
        if not content:
            return tool_error("content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).", success=False)
        result = _create_skill(name, content, category)

    elif action == "edit":
        if not content:
            return tool_error("content is required for 'edit'. Provide the full updated SKILL.md text.", success=False)
        result = _edit_skill(name, content)

    elif action == "patch":
        if not old_string:
            return tool_error("old_string is required for 'patch'. Provide the text to find.", success=False)
        if new_string is None:
            return tool_error("new_string is required for 'patch'. Use empty string to delete matched text.", success=False)
        result = _patch_skill(name, old_string, new_string, file_path, replace_all)

    elif action == "delete":
        result = _delete_skill(name, absorbed_into=absorbed_into)

    elif action == "write_file":
        if not file_path:
            return tool_error("file_path is required for 'write_file'. Example: 'references/api-guide.md'", success=False)
        if file_content is None:
            return tool_error("file_content is required for 'write_file'.", success=False)
        result = _write_file(name, file_path, file_content)

    elif action == "remove_file":
        if not file_path:
            return tool_error("file_path is required for 'remove_file'.", success=False)
        result = _remove_file(name, file_path)

    else:
        result = {"success": False, "error": f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file"}

    if result.get("success"):
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache
            clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception:
            pass
        # Curator telemetry: bump patch_count on edit/patch/write_file (the actions
        # that mutate an existing skill's guidance), drop the record on delete.
        # Only mark a skill as agent-created when the background self-improvement
        # review fork creates it — foreground `skill_manage(create)` calls are
        # user-directed, and those skills belong to the user (the curator must
        # not touch them). Best-effort; telemetry failures never break the tool.
        try:
            from tools.skill_usage import bump_patch, forget, mark_agent_created
            from tools.skill_provenance import is_background_review
            if action == "create":
                if is_background_review():
                    mark_agent_created(name)
            elif action in {"patch", "edit", "write_file", "remove_file"}:
                bump_patch(name)
            elif action == "delete":
                forget(name)
        except Exception:
            pass

    return json.dumps(result, ensure_ascii=False)
```

### 4.2 Validation steps

```python
# =============================================================================
# Validation helpers (referenced by the dispatch table above)
# =============================================================================

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file

# Characters allowed in skill names (filesystem-safe, URL-friendly)
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')

# Subdirectories allowed for write_file/remove_file
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}


def _validate_name(name: str) -> Optional[str]:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_category(category: Optional[str]) -> Optional[str]:
    """Validate an optional category name used as a single directory segment."""
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."

    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """
    Validate that SKILL.md content has proper frontmatter with required fields.
    Returns error message or None if valid.
    """
    if not content.strip():
        return "Content cannot be empty."

    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = content[3:end_match.start() + 3]

    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    """Check that content doesn't exceed the character limit for agent writes.

    Returns an error message or None if within bounds.
    """
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files "
            f"in references/ or templates/."
        )
    return None


def _validate_file_path(file_path: str) -> Optional[str]:
    """
    Validate a file path for write_file/remove_file.
    Must be under an allowed subdirectory and not escape the skill dir.
    """
    from tools.path_security import has_traversal_component

    if not file_path:
        return "file_path is required."

    normalized = Path(file_path)

    # Prevent path traversal
    if has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."

    # Must be under an allowed subdirectory
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"

    # Must have a filename (not just a directory)
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"

    return None


def _resolve_skill_target(skill_dir: Path, file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a supporting-file path and ensure it stays within the skill directory."""
    from tools.path_security import validate_within_dir

    target = skill_dir / file_path
    error = validate_within_dir(target, skill_dir)
    if error:
        return None, error
    return target, None


def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """
    Atomically write text content to a file.

    Uses a temporary file in the same directory and os.replace() to ensure
    the target file is never left in a partially-written state if the process
    crashes or is interrupted.

    Args:
        file_path: Target file path
        content: Content to write
        encoding: Text encoding (default: utf-8)
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        atomic_replace(temp_path, file_path)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            logger.error("Failed to remove temporary file %s during atomic write", temp_path, exc_info=True)
        raise


# =============================================================================
# Per-action handlers
# =============================================================================

def _create_skill(name: str, content: str, category: str = None) -> Dict[str, Any]:
    """Create a new user skill with SKILL.md content."""
    # Validate name
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    # Validate content
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    # Check for name collisions across all directories
    existing = _find_skill(name)
    if existing:
        return {
            "success": False,
            "error": f"A skill named '{name}' already exists at {existing['path']}."
        }

    # Create the skill directory
    skill_dir = _resolve_skill_dir(name, category)
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Write SKILL.md atomically
    skill_md = skill_dir / "SKILL.md"
    _atomic_write_text(skill_md, content)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(skill_dir)
    if scan_error:
        shutil.rmtree(skill_dir, ignore_errors=True)
        return {"success": False, "error": scan_error}

    result = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir.relative_to(SKILLS_DIR)),
        "skill_md": str(skill_md),
    }
    if category:
        result["category"] = category
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        "skill_manage(action='write_file', name='{}', file_path='references/example.md', file_content='...')".format(name)
    )
    return result


def _edit_skill(name: str, content: str) -> Dict[str, Any]:
    """Replace the SKILL.md of any existing skill (full rewrite)."""
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_md = existing["path"] / "SKILL.md"
    # Back up original content for rollback
    original_content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else None
    _atomic_write_text(skill_md, content)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            _atomic_write_text(skill_md, original_content)
        return {"success": False, "error": scan_error}

    return {
        "success": True,
        "message": f"Skill '{name}' updated.",
        "path": str(existing["path"]),
    }


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """Targeted find-and-replace within a skill file.

    Defaults to SKILL.md. Use file_path to patch a supporting file instead.
    Requires a unique match unless replace_all is True.
    """
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use an empty string to delete matched text."}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]

    if file_path:
        # Patching a supporting file
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
    else:
        # Patching SKILL.md
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}

    content = target.read_text(encoding="utf-8")

    # Use the same fuzzy matching engine as the file patch tool.
    # This handles whitespace normalization, indentation differences,
    # escape sequences, and block-anchor matching — saving the agent
    # from exact-match failures on minor formatting mismatches.
    from tools.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    if match_error:
        # Show a short preview of the file so the model can self-correct
        preview = content[:500] + ("..." if len(content) > 500 else "")
        err_msg = match_error
        try:
            from tools.fuzzy_match import format_no_match_hint
            err_msg += format_no_match_hint(match_error, match_count, old_string, content)
        except Exception:
            pass
        return {
            "success": False,
            "error": err_msg,
            "file_preview": preview,
        }

    # Check size limit on the result
    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    # If patching SKILL.md, validate frontmatter is still intact
    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {err}",
            }

    original_content = content  # for rollback
    _atomic_write_text(target, new_content)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(skill_dir)
    if scan_error:
        _atomic_write_text(target, original_content)
        return {"success": False, "error": scan_error}

    return {
        "success": True,
        "message": f"Patched {'SKILL.md' if not file_path else file_path} in skill '{name}' ({match_count} replacement{'s' if match_count > 1 else ''}).",
    }


def _delete_skill(name: str, absorbed_into: Optional[str] = None) -> Dict[str, Any]:
    """Delete a skill.

    ``absorbed_into`` declares intent:
      - ``None`` / missing  → caller didn't declare (legacy / non-curator path);
        accepted for backward compat but logs a warning because the curator
        classification pipeline can't tell consolidation from pruning without it.
      - ``""`` (empty)      → explicit "truly pruned, no forwarding target".
      - ``"<skill-name>"``  → content was absorbed into that umbrella; the
        target must exist on disk. Validated here so the model can't claim an
        umbrella that doesn't exist.
    """
    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    pinned_err = _pinned_guard(name)
    if pinned_err:
        return {"success": False, "error": pinned_err}

    # Validate absorbed_into target when declared non-empty
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        target_name = absorbed_into.strip()
        if target_name == name:
            return {
                "success": False,
                "error": f"absorbed_into='{target_name}' cannot equal the skill being deleted.",
            }
        target = _find_skill(target_name)
        if not target:
            return {
                "success": False,
                "error": (
                    f"absorbed_into='{target_name}' does not exist. "
                    f"Create or patch the umbrella skill first, then retry the delete."
                ),
            }

    skill_dir = existing["path"]
    skills_root = _containing_skills_root(skill_dir)
    shutil.rmtree(skill_dir)

    # Clean up empty category directories (don't remove the skills root itself)
    parent = skill_dir.parent
    if parent != skills_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    message = f"Skill '{name}' deleted."
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        message += f" Content absorbed into '{absorbed_into.strip()}'."

    return {
        "success": True,
        "message": message,
    }


def _write_file(name: str, file_path: str, file_content: str) -> Dict[str, Any]:
    """Add or overwrite a supporting file within any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    if not file_content and file_content != "":
        return {"success": False, "error": "file_content is required."}

    # Check size limits
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                f"Consider splitting into smaller files."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name, " Create it first with action='create'.")}

    target, err = _resolve_skill_target(existing["path"], file_path)
    if err:
        return {"success": False, "error": err}
    target.parent.mkdir(parents=True, exist_ok=True)
    # Back up for rollback
    original_content = target.read_text(encoding="utf-8") if target.exists() else None
    _atomic_write_text(target, file_content)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            _atomic_write_text(target, original_content)
        else:
            target.unlink(missing_ok=True)
        return {"success": False, "error": scan_error}

    return {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }


def _remove_file(name: str, file_path: str) -> Dict[str, Any]:
    """Remove a supporting file from any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if not target.exists():
        # List what's actually there for the model to see
        available = []
        for subdir in ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
        }

    target.unlink()

    # Clean up empty subdirectories
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
    }
```

---

## 5. Skills Guard (verbatim)

### 5.1 Trust + install policy

Source: `/Users/sohamshah/work/services/hermes-agent/tools/skills_guard.py`.

```python
# ---------------------------------------------------------------------------
# Hardcoded trust configuration
# ---------------------------------------------------------------------------

TRUSTED_REPOS = {
    "openai/skills",
    "anthropics/skills",
    "huggingface/skills",
    # NVIDIA-verified skills: each entry ships a signed `skill.oms.sig`
    # and a governance `skill-card.md` (sync pipeline drops anything
    # missing the signature or card). Catalog details:
    # https://github.com/NVIDIA/skills
    "NVIDIA/skills",
}

INSTALL_POLICY = {
    #                  safe      caution    dangerous
    "builtin":       ("allow",  "allow",   "allow"),
    "trusted":       ("allow",  "allow",   "block"),
    "community":     ("allow",  "block",   "block"),
    # Agent-created: "ask" on dangerous surfaces as an error to the agent,
    # which can retry without the flagged content. This gate only runs when
    # skills.guard_agent_created is enabled (off by default) — see
    # tools/skill_manager_tool.py::_guard_agent_created_enabled.
    "agent-created": ("allow",  "allow",   "ask"),
}

VERDICT_INDEX = {"safe": 0, "caution": 1, "dangerous": 2}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    pattern_id: str
    severity: str       # "critical" | "high" | "medium" | "low"
    category: str       # "exfiltration" | "injection" | "destructive" | "persistence" | "network" | "obfuscation"
    file: str
    line: int
    match: str
    description: str


@dataclass
class ScanResult:
    skill_name: str
    source: str
    trust_level: str    # "builtin" | "trusted" | "community"
    verdict: str        # "safe" | "caution" | "dangerous"
    findings: List[Finding] = field(default_factory=list)
    scanned_at: str = ""
    summary: str = ""
```

### 5.2 Threat pattern list (complete)

```python
# ---------------------------------------------------------------------------
# Threat patterns — (regex, pattern_id, severity, category, description)
# ---------------------------------------------------------------------------

THREAT_PATTERNS = [
    # ── Exfiltration: shell commands leaking secrets ──
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_curl", "critical", "exfiltration",
     "curl command interpolating secret environment variable"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_wget", "critical", "exfiltration",
     "wget command interpolating secret environment variable"),
    (r'fetch\s*\([^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)',
     "env_exfil_fetch", "critical", "exfiltration",
     "fetch() call interpolating secret environment variable"),
    (r'httpx?\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_httpx", "critical", "exfiltration",
     "HTTP library call with secret variable"),
    (r'requests\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_requests", "critical", "exfiltration",
     "requests library call with secret variable"),

    # ── Exfiltration: reading credential stores ──
    (r'base64[^\n]*env',
     "encoded_exfil", "high", "exfiltration",
     "base64 encoding combined with environment access"),
    (r'\$HOME/\.ssh|\~/\.ssh',
     "ssh_dir_access", "high", "exfiltration",
     "references user SSH directory"),
    (r'\$HOME/\.aws|\~/\.aws',
     "aws_dir_access", "high", "exfiltration",
     "references user AWS credentials directory"),
    (r'\$HOME/\.gnupg|\~/\.gnupg',
     "gpg_dir_access", "high", "exfiltration",
     "references user GPG keyring"),
    (r'\$HOME/\.kube|\~/\.kube',
     "kube_dir_access", "high", "exfiltration",
     "references Kubernetes config directory"),
    (r'\$HOME/\.docker|\~/\.docker',
     "docker_dir_access", "high", "exfiltration",
     "references Docker config (may contain registry creds)"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env',
     "hermes_env_access", "critical", "exfiltration",
     "directly references Hermes secrets file"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)',
     "read_secrets_file", "critical", "exfiltration",
     "reads known secrets file"),

    # ── Exfiltration: programmatic env access ──
    (r'printenv|env\s*\|',
     "dump_all_env", "high", "exfiltration",
     "dumps all environment variables"),
    (r'os\.environ\b(?!\s*\.get\s*\(\s*["\']PATH)',
     "python_os_environ", "high", "exfiltration",
     "accesses os.environ (potential env dump)"),
    (r'os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)',
     "python_getenv_secret", "critical", "exfiltration",
     "reads secret via os.getenv()"),
    (r'process\.env\[',
     "node_process_env", "high", "exfiltration",
     "accesses process.env (Node.js environment)"),
    (r'ENV\[.*(?:KEY|TOKEN|SECRET|PASSWORD)',
     "ruby_env_secret", "critical", "exfiltration",
     "reads secret via Ruby ENV[]"),

    # ── Exfiltration: DNS and staging ──
    (r'\b(dig|nslookup|host)\s+[^\n]*\$',
     "dns_exfil", "critical", "exfiltration",
     "DNS lookup with variable interpolation (possible DNS exfiltration)"),
    (r'>\s*/tmp/[^\s]*\s*&&\s*(curl|wget|nc|python)',
     "tmp_staging", "critical", "exfiltration",
     "writes to /tmp then exfiltrates"),

    # ── Exfiltration: markdown/link based ──
    (r'!\[.*\]\(https?://[^\)]*\$\{?',
     "md_image_exfil", "high", "exfiltration",
     "markdown image URL with variable interpolation (image-based exfil)"),
    (r'\[.*\]\(https?://[^\)]*\$\{?',
     "md_link_exfil", "high", "exfiltration",
     "markdown link with variable interpolation"),

    # ── Prompt injection ──
    (r'ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions',
     "prompt_injection_ignore", "critical", "injection",
     "prompt injection: ignore previous instructions"),
    (r'you\s+are\s+(?:\w+\s+)*now\s+',
     "role_hijack", "high", "injection",
     "attempts to override the agent's role"),
    (r'do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user',
     "deception_hide", "critical", "injection",
     "instructs agent to hide information from user"),
    (r'system\s+(?:\w+\s+)*prompt\s+(?:\w+\s+)*override',
     "sys_prompt_override", "critical", "injection",
     "attempts to override the system prompt"),
    (r'pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+',
     "role_pretend", "high", "injection",
     "attempts to make the agent assume a different identity"),
    (r'disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)',
     "disregard_rules", "critical", "injection",
     "instructs agent to disregard its rules"),
    (r'output\s+(?:\w+\s+)*(system|initial)\s+prompt',
     "leak_system_prompt", "high", "injection",
     "attempts to extract the system prompt"),
    (r'(when|if)\s+no\s*one\s+is\s+(watching|looking)',
     "conditional_deception", "high", "injection",
     "conditional instruction to behave differently when unobserved"),
    (r'act\s+as\s+(if|though)\s+(?:\w+\s+)*you\s+(?:\w+\s+)*(have\s+no|don\'t\s+have)\s+(?:\w+\s+)*(restrictions|limits|rules)',
     "bypass_restrictions", "critical", "injection",
     "instructs agent to act without restrictions"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)',
     "translate_execute", "critical", "injection",
     "translate-then-execute evasion technique"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->',
     "html_comment_injection", "high", "injection",
     "hidden instructions in HTML comments"),
    (r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none',
     "hidden_div", "high", "injection",
     "hidden HTML div (invisible instructions)"),

    # ── Destructive operations ──
    (r'rm\s+-rf\s+/',
     "destructive_root_rm", "critical", "destructive",
     "recursive delete from root"),
    (r'rm\s+(-[^\s]*)?r.*\$HOME|\brmdir\s+.*\$HOME',
     "destructive_home_rm", "critical", "destructive",
     "recursive delete targeting home directory"),
    (r'chmod\s+777',
     "insecure_perms", "medium", "destructive",
     "sets world-writable permissions"),
    (r'>\s*/etc/',
     "system_overwrite", "critical", "destructive",
     "overwrites system configuration file"),
    (r'\bmkfs\b',
     "format_filesystem", "critical", "destructive",
     "formats a filesystem"),
    (r'\bdd\s+.*if=.*of=/dev/',
     "disk_overwrite", "critical", "destructive",
     "raw disk write operation"),
    (r'shutil\.rmtree\s*\(\s*[\"\'/]',
     "python_rmtree", "high", "destructive",
     "Python rmtree on absolute or root-relative path"),
    (r'truncate\s+-s\s*0\s+/',
     "truncate_system", "critical", "destructive",
     "truncates system file to zero bytes"),

    # ── Persistence ──
    (r'\bcrontab\b',
     "persistence_cron", "medium", "persistence",
     "modifies cron jobs"),
    (r'\.(bashrc|zshrc|profile|bash_profile|bash_login|zprofile|zlogin)\b',
     "shell_rc_mod", "medium", "persistence",
     "references shell startup file"),
    (r'authorized_keys',
     "ssh_backdoor", "critical", "persistence",
     "modifies SSH authorized keys"),
    (r'ssh-keygen',
     "ssh_keygen", "medium", "persistence",
     "generates SSH keys"),
    (r'systemd.*\.service|systemctl\s+(enable|start)',
     "systemd_service", "medium", "persistence",
     "references or enables systemd service"),
    (r'/etc/init\.d/',
     "init_script", "medium", "persistence",
     "references init.d startup script"),
    (r'launchctl\s+load|LaunchAgents|LaunchDaemons',
     "macos_launchd", "medium", "persistence",
     "macOS launch agent/daemon persistence"),
    (r'/etc/sudoers|visudo',
     "sudoers_mod", "critical", "persistence",
     "modifies sudoers (privilege escalation)"),
    (r'git\s+config\s+--global\s+',
     "git_config_global", "medium", "persistence",
     "modifies global git configuration"),

    # ── Network: reverse shells and tunnels ──
    (r'\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b',
     "reverse_shell", "critical", "network",
     "potential reverse shell listener"),
    (r'\bngrok\b|\blocaltunnel\b|\bserveo\b|\bcloudflared\b',
     "tunnel_service", "high", "network",
     "uses tunneling service for external access"),
    (r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}',
     "hardcoded_ip_port", "medium", "network",
     "hardcoded IP address with port"),
    (r'0\.0\.0\.0:\d+|INADDR_ANY',
     "bind_all_interfaces", "high", "network",
     "binds to all network interfaces"),
    (r'/bin/(ba)?sh\s+-i\s+.*>/dev/tcp/',
     "bash_reverse_shell", "critical", "network",
     "bash interactive reverse shell via /dev/tcp"),
    (r'python[23]?\s+-c\s+["\']import\s+socket',
     "python_socket_oneliner", "critical", "network",
     "Python one-liner socket connection (likely reverse shell)"),
    (r'socket\.connect\s*\(\s*\(',
     "python_socket_connect", "high", "network",
     "Python socket connect to arbitrary host"),
    (r'webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com',
     "exfil_service", "high", "network",
     "references known data exfiltration/webhook testing service"),
    (r'pastebin\.com|hastebin\.com|ghostbin\.',
     "paste_service", "medium", "network",
     "references paste service (possible data staging)"),

    # ── Obfuscation: encoding and eval ──
    (r'base64\s+(-d|--decode)\s*\|',
     "base64_decode_pipe", "high", "obfuscation",
     "base64 decodes and pipes to execution"),
    (r'\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}',
     "hex_encoded_string", "medium", "obfuscation",
     "hex-encoded string (possible obfuscation)"),
    (r'\beval\s*\(\s*["\']',
     "eval_string", "high", "obfuscation",
     "eval() with string argument"),
    (r'\bexec\s*\(\s*["\']',
     "exec_string", "high", "obfuscation",
     "exec() with string argument"),
    (r'echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)',
     "echo_pipe_exec", "critical", "obfuscation",
     "echo piped to interpreter for execution"),
    (r'compile\s*\(\s*[^\)]+,\s*["\'].*["\']\s*,\s*["\']exec["\']\s*\)',
     "python_compile_exec", "high", "obfuscation",
     "Python compile() with exec mode"),
    (r'getattr\s*\(\s*__builtins__',
     "python_getattr_builtins", "high", "obfuscation",
     "dynamic access to Python builtins (evasion technique)"),
    (r'__import__\s*\(\s*["\']os["\']\s*\)',
     "python_import_os", "high", "obfuscation",
     "dynamic import of os module"),
    (r'codecs\.decode\s*\(\s*["\']',
     "python_codecs_decode", "medium", "obfuscation",
     "codecs.decode (possible ROT13 or encoding obfuscation)"),
    (r'String\.fromCharCode|charCodeAt',
     "js_char_code", "medium", "obfuscation",
     "JavaScript character code construction (possible obfuscation)"),
    (r'atob\s*\(|btoa\s*\(',
     "js_base64", "medium", "obfuscation",
     "JavaScript base64 encode/decode"),
    (r'\[::-1\]',
     "string_reversal", "low", "obfuscation",
     "string reversal (possible obfuscated payload)"),
    (r'chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+',
     "chr_building", "high", "obfuscation",
     "building string from chr() calls (obfuscation)"),
    (r'\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}',
     "unicode_escape_chain", "medium", "obfuscation",
     "chain of unicode escapes (possible obfuscation)"),

    # ── Process execution in scripts ──
    (r'subprocess\.(run|call|Popen|check_output)\s*\(',
     "python_subprocess", "medium", "execution",
     "Python subprocess execution"),
    (r'os\.system\s*\(',
     "python_os_system", "high", "execution",
     "os.system() — unguarded shell execution"),
    (r'os\.popen\s*\(',
     "python_os_popen", "high", "execution",
     "os.popen() — shell pipe execution"),
    (r'child_process\.(exec|spawn|fork)\s*\(',
     "node_child_process", "high", "execution",
     "Node.js child_process execution"),
    (r'Runtime\.getRuntime\(\)\.exec\(',
     "java_runtime_exec", "high", "execution",
     "Java Runtime.exec() — shell execution"),
    (r'`[^`]*\$\([^)]+\)[^`]*`',
     "backtick_subshell", "medium", "execution",
     "backtick string with command substitution"),

    # ── Path traversal ──
    (r'\.\./\.\./\.\.',
     "path_traversal_deep", "high", "traversal",
     "deep relative path traversal (3+ levels up)"),
    (r'\.\./\.\.',
     "path_traversal", "medium", "traversal",
     "relative path traversal (2+ levels up)"),
    (r'/etc/passwd|/etc/shadow',
     "system_passwd_access", "critical", "traversal",
     "references system password files"),
    (r'/proc/self|/proc/\d+/',
     "proc_access", "high", "traversal",
     "references /proc filesystem (process introspection)"),
    (r'/dev/shm/',
     "dev_shm", "medium", "traversal",
     "references shared memory (common staging area)"),

    # ── Crypto mining ──
    (r'xmrig|stratum\+tcp|monero|coinhive|cryptonight',
     "crypto_mining", "critical", "mining",
     "cryptocurrency mining reference"),
    (r'hashrate|nonce.*difficulty',
     "mining_indicators", "medium", "mining",
     "possible cryptocurrency mining indicators"),

    # ── Supply chain: curl/wget pipe to shell ──
    (r'curl\s+[^\n]*\|\s*(ba)?sh',
     "curl_pipe_shell", "critical", "supply_chain",
     "curl piped to shell (download-and-execute)"),
    (r'wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh',
     "wget_pipe_shell", "critical", "supply_chain",
     "wget piped to shell (download-and-execute)"),
    (r'curl\s+[^\n]*\|\s*python',
     "curl_pipe_python", "critical", "supply_chain",
     "curl piped to Python interpreter"),

    # ── Supply chain: unpinned/deferred dependencies ──
    (r'#\s*///\s*script.*dependencies',
     "pep723_inline_deps", "medium", "supply_chain",
     "PEP 723 inline script metadata with dependencies (verify pinning)"),
    (r'pip\s+install\s+(?!-r\s)(?!.*==)',
     "unpinned_pip_install", "medium", "supply_chain",
     "pip install without version pinning"),
    (r'npm\s+install\s+(?!.*@\d)',
     "unpinned_npm_install", "medium", "supply_chain",
     "npm install without version pinning"),
    (r'uv\s+run\s+',
     "uv_run", "medium", "supply_chain",
     "uv run (may auto-install unpinned dependencies)"),

    # ── Supply chain: remote resource fetching ──
    (r'(curl|wget|httpx?\.get|requests\.get|fetch)\s*[\(]?\s*["\']https?://',
     "remote_fetch", "medium", "supply_chain",
     "fetches remote resource at runtime"),
    (r'git\s+clone\s+',
     "git_clone", "medium", "supply_chain",
     "clones a git repository at runtime"),
    (r'docker\s+pull\s+',
     "docker_pull", "medium", "supply_chain",
     "pulls a Docker image at runtime"),

    # ── Privilege escalation ──
    (r'^allowed-tools\s*:',
     "allowed_tools_field", "high", "privilege_escalation",
     "skill declares allowed-tools (pre-approves tool access)"),
    (r'\bsudo\b',
     "sudo_usage", "high", "privilege_escalation",
     "uses sudo (privilege escalation)"),
    (r'setuid|setgid|cap_setuid',
     "setuid_setgid", "critical", "privilege_escalation",
     "setuid/setgid (privilege escalation mechanism)"),
    (r'NOPASSWD',
     "nopasswd_sudo", "critical", "privilege_escalation",
     "NOPASSWD sudoers entry (passwordless privilege escalation)"),
    (r'chmod\s+[u+]?s',
     "suid_bit", "critical", "privilege_escalation",
     "sets SUID/SGID bit on a file"),

    # ── Agent config persistence ──
    (r'AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules',
     "agent_config_mod", "critical", "persistence",
     "references agent config files (could persist malicious instructions across sessions)"),
    (r'\.hermes/config\.yaml|\.hermes/SOUL\.md',
     "hermes_config_mod", "critical", "persistence",
     "references Hermes configuration files directly"),
    (r'\.claude/settings|\.codex/config',
     "other_agent_config", "high", "persistence",
     "references other agent configuration files"),

    # ── Hardcoded secrets (credentials embedded in the skill itself) ──
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\'][A-Za-z0-9+/=_-]{20,}',
     "hardcoded_secret", "critical", "credential_exposure",
     "possible hardcoded API key, token, or secret"),
    (r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----',
     "embedded_private_key", "critical", "credential_exposure",
     "embedded private key"),
    (r'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}',
     "github_token_leaked", "critical", "credential_exposure",
     "GitHub personal access token in skill content"),
    (r'sk-[A-Za-z0-9]{20,}',
     "openai_key_leaked", "critical", "credential_exposure",
     "possible OpenAI API key in skill content"),
    (r'sk-ant-[A-Za-z0-9_-]{90,}',
     "anthropic_key_leaked", "critical", "credential_exposure",
     "possible Anthropic API key in skill content"),
    (r'AKIA[0-9A-Z]{16}',
     "aws_access_key_leaked", "critical", "credential_exposure",
     "AWS access key ID in skill content"),

    # ── Additional prompt injection: jailbreak patterns ──
    (r'\bDAN\s+mode\b|Do\s+Anything\s+Now',
     "jailbreak_dan", "critical", "injection",
     "DAN (Do Anything Now) jailbreak attempt"),
    (r'\bdeveloper\s+mode\b.*\benabled?\b',
     "jailbreak_dev_mode", "critical", "injection",
     "developer mode jailbreak attempt"),
    (r'hypothetical\s+scenario.*(?:ignore|bypass|override)',
     "hypothetical_bypass", "high", "injection",
     "hypothetical scenario used to bypass restrictions"),
    (r'for\s+educational\s+purposes?\s+only',
     "educational_pretext", "medium", "injection",
     "educational pretext often used to justify harmful content"),
    (r'(respond|answer|reply)\s+without\s+(?:\w+\s+)*(restrictions|limitations|filters|safety)',
     "remove_filters", "critical", "injection",
     "instructs agent to respond without safety filters"),
    (r'you\s+have\s+been\s+(?:\w+\s+)*(updated|upgraded|patched)\s+to',
     "fake_update", "high", "injection",
     "fake update/patch announcement (social engineering)"),
    (r'new\s+(?:\w+\s+)*policy|updated\s+(?:\w+\s+)*guidelines|revised\s+(?:\w+\s+)*instructions',
     "fake_policy", "medium", "injection",
     "claims new policy/guidelines (may be social engineering)"),

    # ── Context window exfiltration ──
    (r'(include|output|print|send|share)\s+(?:\w+\s+)*(conversation|chat\s+history|previous\s+messages|context)',
     "context_exfil", "high", "exfiltration",
     "instructs agent to output/share conversation history"),
    (r'(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://',
     "send_to_url", "high", "exfiltration",
     "instructs agent to send data to a URL"),
]

# Structural limits for skill directories
MAX_FILE_COUNT = 50       # skills shouldn't have 50+ files
MAX_TOTAL_SIZE_KB = 1024  # 1MB total is suspicious for a skill
MAX_SINGLE_FILE_KB = 256  # individual file > 256KB is suspicious

# File extensions to scan (text files only — skip binary)
SCANNABLE_EXTENSIONS = {
    '.md', '.txt', '.py', '.sh', '.bash', '.js', '.ts', '.rb',
    '.yaml', '.yml', '.json', '.toml', '.cfg', '.ini', '.conf',
    '.html', '.css', '.xml', '.tex', '.r', '.jl', '.pl', '.php',
}

# Known binary extensions that should NOT be in a skill
SUSPICIOUS_BINARY_EXTENSIONS = {
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.com',
    '.msi', '.dmg', '.app', '.deb', '.rpm',
}

# Zero-width and invisible unicode characters used for injection
INVISIBLE_CHARS = {
    '​',  # zero-width space
    '‌',  # zero-width non-joiner
    '‍',  # zero-width joiner
    '⁠',  # word joiner
    '⁢',  # invisible times
    '⁣',  # invisible separator
    '⁤',  # invisible plus
    '﻿',  # zero-width no-break space (BOM)
    '‪',  # left-to-right embedding
    '‫',  # right-to-left embedding
    '‬',  # pop directional formatting
    '‭',  # left-to-right override
    '‮',  # right-to-left override
    '⁦',  # left-to-right isolate
    '⁧',  # right-to-left isolate
    '⁨',  # first strong isolate
    '⁩',  # pop directional isolate
}
```

### 5.3 Scanner functions

Source: `/Users/sohamshah/work/services/hermes-agent/tools/skills_guard.py`.

```python
def scan_skill(skill_path: Path, source: str = "community") -> ScanResult:
    """
    Scan all files in a skill directory for security threats.

    Performs:
    1. Structural checks (file count, total size, binary files, symlinks)
    2. Regex pattern matching on all text files
    3. Invisible unicode character detection

    Args:
        skill_path: Path to the skill directory (must contain SKILL.md)
        source: Source identifier for trust level resolution (e.g. "openai/skills")

    Returns:
        ScanResult with verdict, findings, and trust metadata
    """
    skill_name = skill_path.name
    trust_level = _resolve_trust_level(source)

    all_findings: List[Finding] = []

    if skill_path.is_dir():
        # Structural checks first
        all_findings.extend(_check_structure(skill_path))

        # Pattern scanning on each file
        for f in skill_path.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(skill_path))
                all_findings.extend(scan_file(f, rel))
    elif skill_path.is_file():
        all_findings.extend(scan_file(skill_path, skill_path.name))

    verdict = _determine_verdict(all_findings)
    summary = _build_summary(skill_name, source, trust_level, verdict, all_findings)

    return ScanResult(
        skill_name=skill_name,
        source=source,
        trust_level=trust_level,
        verdict=verdict,
        findings=all_findings,
        scanned_at=datetime.now(timezone.utc).isoformat(),
        summary=summary,
    )


def should_allow_install(result: ScanResult, force: bool = False) -> Tuple[bool, str]:
    """
    Determine whether a skill should be installed based on scan result and trust.

    Args:
        result: Scan result from scan_skill()
        force: If True, override blocked policy decisions for this scan result

    Returns:
        (allowed, reason) tuple
    """
    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["community"])
    vi = VERDICT_INDEX.get(result.verdict, 2)
    decision = policy[vi]

    if decision == "allow":
        return True, f"Allowed ({result.trust_level} source, {result.verdict} verdict)"

    if force and not (result.verdict == "dangerous" and result.trust_level in ("community", "trusted")):
        return True, (
            f"Force-installed despite {result.verdict} verdict "
            f"({len(result.findings)} findings)"
        )

    if decision == "ask":
        # Return None to signal "needs user confirmation"
        return None, (
            f"Requires confirmation ({result.trust_level} source + {result.verdict} verdict, "
            f"{len(result.findings)} findings)"
        )

    # Dangerous verdicts cannot be overridden by --force (community/trusted);
    # other blocks can.
    if result.verdict == "dangerous" and result.trust_level in ("community", "trusted"):
        return False, (
            f"Blocked ({result.trust_level} source + dangerous verdict, "
            f"{len(result.findings)} findings). --force does not override a dangerous verdict."
        )
    return False, (
        f"Blocked ({result.trust_level} source + {result.verdict} verdict, "
        f"{len(result.findings)} findings). Use --force to override."
    )


def format_scan_report(result: ScanResult) -> str:
    """
    Format a scan result as a human-readable report string.

    Returns a compact multi-line report suitable for CLI or chat display.
    """
    lines = []

    verdict_display = result.verdict.upper()
    lines.append(f"Scan: {result.skill_name} ({result.source}/{result.trust_level})  Verdict: {verdict_display}")

    if result.findings:
        # Group and sort: critical first, then high, medium, low
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_findings = sorted(result.findings, key=lambda f: severity_order.get(f.severity, 4))

        for f in sorted_findings:
            sev = f.severity.upper().ljust(8)
            cat = f.category.ljust(14)
            loc = f"{f.file}:{f.line}".ljust(30)
            lines.append(f"  {sev} {cat} {loc} \"{f.match[:60]}\"")

        lines.append("")

    allowed, reason = should_allow_install(result)
    if allowed is True:
        status = "ALLOWED"
    elif allowed is None:
        status = "NEEDS CONFIRMATION"
    else:
        status = "BLOCKED"
    lines.append(f"Decision: {status} — {reason}")

    return "\n".join(lines)
```

Verdict computation rules implicit in the design notes:
- `_determine_verdict`: any `"critical"` → `"dangerous"`; else any `"high"` → `"caution"`; else `"safe"`. Medium/low are informational only.
- Trust resolution in `_resolve_trust_level` MUST be exact-match OR `trusted/` prefix-with-slash; plain `startswith("openai/skills")` would let `openai/skills-evil` masquerade as trusted.
- `should_allow_install` is **tristate**: `True | False | None` (None = "ask"); callers MUST handle the `None` branch explicitly — treating it as falsy would silently unlock dangerous agent-created skills.

---

## 6. Usage sidecar (verbatim)

The artifact set provided does not include `tools/skill_usage.py` or `tools/skill_provenance.py` source. The contract is fully implied by the dispatch-table call site in section 4.1:

```python
from tools.skill_usage import bump_patch, forget, mark_agent_created
from tools.skill_provenance import is_background_review
if action == "create":
    if is_background_review():
        mark_agent_created(name)
elif action in {"patch", "edit", "write_file", "remove_file"}:
    bump_patch(name)
elif action == "delete":
    forget(name)
```

### 6.1 Schema

A per-skill record keyed by skill name with at least:
- `patch_count: int` — bumped by `bump_patch` on any mutating action
- `agent_created: bool` — set by `mark_agent_created` only when `is_background_review()` is true (foreground `skill_manage(create)` calls are user-directed and must NOT be marked agent-created, otherwise the curator will auto-prune user skills)
- presumably timestamps and last-touched provenance, consumed by the curator's classification pipeline (along with `absorbed_into` from `_delete_skill`)

### 6.2 Counter bump functions

- `bump_patch(name)` — increment `patch_count` for `name`; called on `patch`/`edit`/`write_file`/`remove_file`. All four mutate existing-skill guidance; `create` is excluded because the record is brand new and `delete` because it's about to be dropped.
- `forget(name)` — drop the record entirely; called on `delete`. Paired with `_delete_skill`'s `absorbed_into` semantics so the curator can distinguish prune vs. consolidate later from its own logs.
- `mark_agent_created(name)` — set the agent-created flag; called on `create` **only when** `is_background_review()` returns true. `is_background_review()` reads thread-local provenance (set on the review fork via `_memory_write_origin = "background_review"` in 2.2) — porting requires an equivalent thread-local probe.

### 6.3 Atomic I/O pattern

The artifact set does not include `skill_usage`'s file I/O, but the canonical atomic-write pattern used throughout Hermes is `_atomic_write_text` from `skill_manager_tool.py` (see section 4.2):

```python
def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        atomic_replace(temp_path, file_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            logger.error("Failed to remove temporary file %s during atomic write", temp_path, exc_info=True)
        raise
```

Pattern: temp file in the same directory (so `os.replace` is atomic on POSIX), `os.fdopen` on the mkstemp fd (no race), `atomic_replace` for the final rename, cleanup-on-exception so crashes don't leave `.tmp.` litter. The skill-usage sidecar must use the same pattern or concurrent writes from two background review threads (rare but possible) will corrupt the file.

---

## 7. Port notes (synthesis for Freyja)

### 7.1 SKILL_REVIEW_PROMPT → Freyja

Strip the Hermes-specific tool/CLI references and replace with Freyja equivalents: `skill_manage action=write_file` → Freyja's skill-write action name; `skills_list` / `skill_view` → Freyja's discovery + read tools; `/skill-name` slash convention → whatever Freyja's load-on-demand syntax is; `'hermes skills install'` and `'hermes curator pin'` → Freyja CLI strings; `'hermes-agent'` (the bundled-skill example) → Freyja's bundled-skill namespace. Keep verbatim: the four numbered preference-order paragraphs, the three support-file directory taxonomy (`references/` / `templates/` / `scripts/`), the "Do NOT capture" denylist (section 1.6), the protected-vs-pinned distinction, and the explicit "Nothing to save." sentinel string (the summarizer parses this). The prompt is concatenated adjacent string literals in Python; if porting to a different language, concatenate in order to get the single string the LLM sees.

### 7.2 MEMORY_REVIEW_PROMPT → Freyja

Smallest of the three — port verbatim. The only Freyja-specific concern is the runtime suffix added by `_run_review_in_thread` (`"You can only call memory and skill management tools..."`); Freyja's spawn site must append an equivalent suffix or bake it into the constant. Preserve the em-dash (—) and curly quotes; preserve the exact "Nothing to save." string.

### 7.3 COMBINED_REVIEW_PROMPT → Freyja

Same rename surface as SKILL_REVIEW_PROMPT (CLI names, tool names, slash convention, support-file directory contract). The "Act on whichever of the two dimensions has real signal" closing is the critical anti-default framing — keep it; if Freyja's review fork tends to no-op, this is the lever to tighten.

### 7.4 MEMORY_GUIDANCE → Freyja

The text references `memory` tool, `session_search`, and the `skill` tool / `skill_manage`. Freyja must have an equivalent past-transcript recall tool (or rewrite the sentence to omit it — but DON'T leave a dangling reference to a nonexistent tool, the model will try to call it). The 7-day TTL heuristic and ✓/✗ examples are model-agnostic and should be preserved verbatim. The declarative-not-imperative rule is the load-bearing defense against re-read-as-directive bugs in future sessions.

### 7.5 SKILLS_GUIDANCE → Freyja

Rename `skill_manage` and `skill_manage(action='patch')` to the Freyja equivalents in lockstep. The "5+ tool calls / tricky error / non-trivial workflow" trigger threshold is a generous bar that reduces agent self-negotiation — keep it. The "Skills that aren't maintained become liabilities" closing frames neglect as actively harmful; preserve the framing because softer wording produces measurably less patch-on-touch behavior.

### 7.6 Fork construction (section 2) → Freyja

This is the biggest porting surface. Hermes spawns a forked `AIAgent` from `run_agent`; Freyja's analogous primitive is `_BridgeSession` running over `_BridgeState`. The port should:

- **Replace `AIAgent(...)` with a `_BridgeSession` constructor or factory** that accepts the same shape of arguments: model, provider, api_mode, base_url, api_key, credential_pool, parent_session_id, toolset config, `skip_memory`-equivalent flag.
- **Drop the codex_app_server downgrade** — Freyja has no codex_app_server. Keep the underlying principle though: any runtime that bypasses Freyja's tool dispatcher must be downgraded to one that doesn't, or memory/skill tools won't dispatch.
- **Drop the Honcho / Hindsight / mem0 / supermemory references** — Freyja doesn't have those external memory plugins. The `skip_memory=True` flag's purpose collapses to "don't rebuild any per-session external memory state" — likely a no-op for Freyja, but keep the re-binding of `_memory_store` / `_memory_enabled` / `_user_profile_enabled` to whatever Freyja calls its `~/.freyja/MEMORY.md` and `~/.freyja/USER.md` stores.
- **Replace `hermes_cli.plugins.set_thread_tool_whitelist` / `clear_thread_tool_whitelist`** with Freyja's equivalent thread-local dispatch guard. If Freyja doesn't have one, build it — the design notes are explicit that prompt-only restrictions are insufficient and that runtime denial is what makes the addendum to the user-message credible.
- **Reuse Freyja's stdout/stderr handling**: the `contextlib.redirect_stdout` block wraps the entire fork lifetime; Freyja's bridge sinks likely already have a "quiet mode" — check whether `suppress_status_output` has a built-in analog (the desktop sink modification in current uncommitted changes may already touch this).
- **Re-implement `_safe_print` / `background_review_callback`** as Freyja's bridge-emit path so the "💾 Self-improvement review: ..." line reaches the user. Slack and desktop sinks should both render this.
- **Preserve the daemon-thread spawn shape**: `threading.Thread(target=..., daemon=True).start()`. The parent must not block on the review; the review's only output is the post-completion summary line.
- **Approval-callback install (`_bg_review_auto_deny`)**: only relevant if Freyja has a prompt_toolkit-style interactive approval gate. The desktop sink almost certainly does — port the auto-deny to avoid deadlocking against the parent TUI.
- **`_cached_system_prompt` inheritance**: identify Freyja's equivalent cached-prompt attribute (likely on `_BridgeSession` or `_BridgeState`). Without this assignment, the prefix-cache hit rate craters by ~26%.

The `summarize_background_review_actions` function is fully self-contained and can be ported almost verbatim — it only needs the message-shape contract (`role == "tool"`, JSON content with `success`/`message`/`target` fields). Match Freyja's skill/memory tool output schemas to this, or rewrite the keyword cascade.

### 7.7 Cadence counters → Freyja

The counter init pattern (defaults first, then config-read inside try/except) is universal Python defensive style and should port directly into Freyja's `_BridgeSession.__init__` (or wherever per-session state is initialized). Place the four fields (`_turns_since_memory`, `_iters_since_skill`, `_memory_nudge_interval`, `_skill_nudge_interval`) on `_BridgeSession`. The YAML key paths (`agent.memory.nudge_interval`, `agent.skills.creation_nudge_interval`) need Freyja-equivalent paths — likely `~/.freyja/config.yaml` if Freyja uses the same TOML/YAML config convention. The increment/trip-check sites need to be wired into Freyja's per-turn and per-iteration hooks; the spawn-site selection logic (memory-only → MEMORY_PROMPT, skill-only → SKILL_PROMPT, both → COMBINED_PROMPT) is straightforward boolean dispatch.

### 7.8 Skill management tool → Freyja

Port `skill_manage` more-or-less verbatim into Freyja's tool registry. Substitutions: `from agent.prompt_builder import clear_skills_system_prompt_cache` → Freyja's skill-prompt cache invalidator (if no equivalent exists, build one — without cache invalidation, agents will see stale skill listings until the next bridge restart). The fuzzy-match dependency (`tools.fuzzy_match.fuzzy_find_and_replace`) is high-value and worth porting wholesale — exact-match patches fail constantly on whitespace/indent mismatches.

**Critical**: keep error message wording verbatim. Hermes-trained agents (and any LLM trained on Hermes traces) have learned to self-correct from the exact strings "SKILL.md must start with YAML frontmatter (---)", "Path traversal ('..') is not allowed.", "File must be under one of: assets, references, scripts, templates." Changing the wording invalidates this prompt-following behavior. The `ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}` set must match the three directories named in the SKILL_REVIEW_PROMPT (the prompt only names three; `"assets"` is a fourth tolerated but unmentioned bucket — preserve this asymmetry).

The `_delete_skill` `absorbed_into` tristate (None / "" / non-empty-must-resolve) must be preserved — the curator's classification pipeline (whatever Freyja's curator looks like) depends on the distinction between "legacy delete" and "explicit prune" and "absorb into existing umbrella". Do NOT collapse to a boolean.

The Freyja filesystem layout for skills: replace Hermes' `SKILLS_DIR` constant with `~/.freyja/skills/` (or wherever Freyja's session/state directory lives — match the `~/.freyja` convention noted in the user's brief). The multi-root lookup (`_find_skill` traverses local + external_dirs) is a feature the agent depends on; if Freyja is single-root, the dispatcher logic simplifies, but reconsider whether you want hub-installed-skill support later.

### 7.9 Skills Guard → Freyja

Port the trust/policy/threat-pattern tables verbatim with three substitutions:
1. `TRUSTED_REPOS`: keep the existing OpenAI/Anthropic/HuggingFace/NVIDIA entries (universal good defaults), and add the Freyja equivalent of `anthropics/skills` if there's a Freyja-hub equivalent.
2. `hermes_env_access` pattern (`r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env'`) → `r'\$HOME/\.freyja/\.env|\~/\.freyja/\.env'`.
3. `hermes_config_mod` pattern (`r'\.hermes/config\.yaml|\.hermes/SOUL\.md'`) → `r'\.freyja/config\.yaml|\.freyja/<analog-to-SOUL.md>'`. Find what Freyja's persona/identity file is called (Hermes' SOUL.md is the agent's character file).

Critical preserves: the regex set is matched case-insensitive per-line (not multiline) — don't change the matching mode without re-auditing every pattern. The `_resolve_trust_level` exact-OR-slash-prefix check is the only thing preventing `openai/skills-evil` from masquerading as trusted. The `should_allow_install` tristate (True/None/False) must be handled by callers — None means "ask"; treating it as falsy silently unlocks agent-created dangerous skills.

The 16 invisible-unicode characters in `INVISIBLE_CHARS` MUST be preserved as UTF-8 byte sequences; don't let an editor normalize them. The `dataclass` shapes (`Finding`, `ScanResult`) port directly.

### 7.10 Usage sidecar → Freyja

Implement `bump_patch` / `forget` / `mark_agent_created` over a JSON file (or sqlite) at `~/.freyja/skill_usage.json`. Use the atomic-write pattern from section 6.3 to avoid corruption under concurrent background reviews. `is_background_review()` reads thread-local provenance — wire it to whatever Freyja sets on the review fork (the equivalent of Hermes' `_memory_write_origin = "background_review"`). The classification semantic — only background-review `create` is auto-prunable, foreground user `create` is sacred — is the single most important invariant; getting this wrong means the curator will eat skills the user typed in by hand.

### 7.11 Things Freyja already has — don't re-port

- **Bridge sinks**: the desktop and Slack sinks (per recent commits) already have proven patterns for streaming, compaction, and quiet/loud emit modes. The `agent._safe_print` + `background_review_callback` pair from section 2.5 should bind to existing bridge-emit primitives, not a new code path.
- **Scheduler**: the durable-scheduled-jobs system in `bridge/scheduler/` (recent commits `da24bce`) likely has the right thread-local + atomic-state patterns to model the cadence-counter persistence on, if you want counters to survive bridge restarts.
- **Image/content sanitizer**: separate concern from skill-content security scanning, but if Freyja already has a content-sanitizer registry, the skills-guard scanner can register as another sanitizer module rather than a standalone tool.

### 7.12 Things to confirm before merging

- Does Freyja's tool-dispatch system support a thread-local denylist/whitelist? If not, the runtime tool whitelist in section 2.4 needs new infrastructure.
- Where does Freyja cache the system prompt? The `_cached_system_prompt` inheritance is worth ~26% cost on long sessions.
- Does Freyja have a circular-import risk between `_BridgeSession` and the skill-management tool? Hermes uses local imports inside `_run_review_in_thread` to avoid this; mirror that if needed.
- What's the equivalent of `is_background_review()` thread-local probe in Freyja? Without it, every `skill_manage(create)` call gets marked agent-created and the curator eats user skills.