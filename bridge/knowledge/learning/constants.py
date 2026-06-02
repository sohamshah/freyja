"""Centralized constants for the skill-learning loop.

Every tuneable threshold, default model id, env var name, byte cap,
window size, and TTL lives here so they can be found, reviewed, and
adjusted in one place. The rest of the learning module imports from
this file; no constants should be redeclared inline.

Convention:
  · Operator-facing knobs live under "Tunables" with the env var that
    overrides them (when one exists).
  · Implementation-detail caps live under "Caps & windows".
  · Names + paths used as event/string tokens live under "String
    tokens" (these are part of the on-disk contract — bumping any
    name field requires a schema-migration plan).

Anything that's domain *data* rather than a knob — the 12 outcome
category names, the Skills Guard severity ranking, the 120-entry
threat pattern table, prompt strings — stays in its domain module.
Constants are the kind of value you'd point a teammate at and ask
"is 5 still right?" — they belong here.
"""

from __future__ import annotations


# ============================================================================
# Tunables — operator-facing knobs
# ============================================================================

# Cadence — how many user turns between automatic drafter trips.
# Workspace-global counter on disk; trip resets to 0. Drafter
# spawn is now an agentic sub-agent (multi-iteration Opus), so the
# default is conservative: ~3-5 trips per active workday at typical
# usage. Override with FREYJA_SKILL_NUDGE_INTERVAL=N. <=0 disables
# automatic trips; operator-issued /learn-this still works.
CADENCE_DEFAULT_THRESHOLD = 5
CADENCE_THRESHOLD_ENV_VAR = "FREYJA_SKILL_NUDGE_INTERVAL"

# Drafter model — used both by the legacy single-call drafter (kept as
# the prompt-cache anchor + fallback) and surfaced to the
# skill-drafter AgentType for telemetry. The actual sub-agent model
# selection happens in bridge/tools/agent_types.py — this constant
# governs the env-override + fallback path only.
DRAFTER_DEFAULT_MODEL = "claude-opus-4-8"
DRAFTER_MODEL_ENV_VAR = "FREYJA_DRAFTER_MODEL"

# Outcome classifier model. Same shape: default + env override.
OUTCOME_CLASSIFIER_DEFAULT_MODEL = "claude-opus-4-8"
OUTCOME_CLASSIFIER_MODEL_ENV_VAR = "FREYJA_OUTCOME_CLASSIFIER_MODEL"


# ============================================================================
# Overwrite-destructive thresholds
# ============================================================================
# Both conditions are OR-ed: a candidate is flagged ``isDestructive``
# when removed_lines >= the absolute threshold OR when removed_lines /
# existing_lines >= the ratio. SkillToast then renames PROMOTE to
# "REPLACE -N" and requires a double-tap to confirm.
#
# Calibration rationale: the first ema-release-ops draft replaced a
# 404-line skill with a 120-line summary, deleting ~280 lines. That's
# the failure mode the constants protect against. 100 lines + 50%
# means a routine 30-line tweak to a 100-line skill is non-
# destructive, but a 60-line rewrite of the same skill is destructive.

OVERWRITE_DESTRUCTIVE_LINES = 100
OVERWRITE_DESTRUCTIVE_RATIO = 0.5


# ============================================================================
# Caps & windows
# ============================================================================

# Outcome watcher: how many turns after a skill load to wait before
# classifying. Window includes the load turn + N subsequent turns.
# Short by design — the classification is about what happened RIGHT
# AFTER the skill arrived, not what happened all session.
POST_LOAD_WINDOW_TURNS = 3

# Char cap on the post-load window string the classifier sees. Bigger
# than the prompt-cache prefix; meant to keep classifier prompt size
# bounded for a chatty turn that drops 100KB+ of tool output.
POST_LOAD_WINDOW_MAX_CHARS = 12_000

# TTL on pending classification records that never accumulated a
# single post-load turn. Empty pending records from sessions that
# crashed at load+0 get garbage-collected at this age. 24h matches
# the rough "operator returns next day, expects stale state cleaned"
# expectation.
PENDING_EMPTY_TTL_MS = 24 * 60 * 60 * 1000

# Max body chars sent to the outcome classifier. The full SKILL.md
# can be large; the classifier doesn't need the entire body to grade
# a load — the description + first few sections cover most cases.
OUTCOME_CLASSIFIER_MAX_SKILL_BODY_CHARS = 8000

# Drafter: how many skill names from the library to enumerate in the
# user-message prompt block. Older sessions had 50+ skills and the
# enumeration alone added 4-5k context tokens; capping bounds it.
DRAFTER_MAX_LISTED_SKILLS = 50

# Free-text field truncation cap for event log entries. The append-
# only events.jsonl design depends on single-write atomicity (POSIX
# PIPE_BUF, 4KB on Linux / 512B on macOS). Long evidence quotes /
# rationales would push a single line past PIPE_BUF and interleave
# with concurrent writers. 256 chars per field keeps a 200-byte
# event well under 4KB even with both fields populated.
EVENT_FREE_TEXT_MAX_CHARS = 256

# Fields the events writer truncates. Structural fields (skill,
# session_id, category, ts) stay verbatim — they're bounded by
# definition.
EVENT_TRUNCATED_FIELDS: tuple[str, ...] = (
    "evidence",
    "summary",
    "rationale",
    "note",
    "load_context",
)

# Value rollup: rolling window size (number of most-recent outcome
# events that count toward V). Older events are ignored so a skill
# that was bad early but is good now isn't dragged down forever.
VALUE_ROLLING_WINDOW = 30

# In-process cache size for value rollups, keyed by (skill, events_mtime).
# Bumps to a new entry whenever the events log changes.
VALUE_ROLLUP_CACHE_CAP = 256


# ============================================================================
# String tokens — on-disk contract
# ============================================================================

# These are tokens that appear on disk (in events.jsonl, in candidate
# YAML files, in pending records) AND in the renderer's TS reducer.
# Renaming any of these without coordinating both ends is a schema
# migration.

# Drafter-decision result enum. Used by review_worker (which writes
# the audit event) and by drafter.py (which emits the renderer event).
DRAFTER_RESULT_CANDIDATE = "candidate"
DRAFTER_RESULT_SKIP = "skip"
DRAFTER_RESULT_ERROR = "error"
