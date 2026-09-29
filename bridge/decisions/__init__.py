"""Decision layer: typed, calibrated, logged answers for harness decisions.

Usage from harness code::

    from bridge.decisions import DecisionClient

    client = DecisionClient()                      # providers from env, specs from ./specs
    d = await client.decide("command_risk", {"command": cmd, "cwd": cwd})
    if d.effective_action == "escalate":           # only ever tightens
        level = max(level, PermissionLevel.HIGH)

The model never owns control flow and never generates text. See
docs in the Jev field report for the rules each spec must follow.
"""

from bridge.decisions.client import Decision, DecisionClient, DecisionUnavailable
from bridge.decisions.log import DecisionLog
from bridge.decisions.provider import DecisionError, DecisionProvider, TypeSafeProvider, providers_from_env
from bridge.decisions.spec import DecisionSpec, Gate, Verdict, evaluate_gate, load_spec, load_specs
from bridge.decisions.types import (
    Answers,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
)

__all__ = [
    "Answers",
    "Choice",
    "ChoiceAnswer",
    "Decision",
    "DecisionClient",
    "DecisionError",
    "DecisionLog",
    "DecisionProvider",
    "DecisionSpec",
    "DecisionUnavailable",
    "Gate",
    "Noul",
    "NoulAnswer",
    "Question",
    "Score",
    "ScoreAnswer",
    "TypeSafeProvider",
    "Verdict",
    "evaluate_gate",
    "load_spec",
    "load_specs",
    "providers_from_env",
]
