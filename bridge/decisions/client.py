"""DecisionClient: one entry point for harness code to ask a typed question
battery about a piece of state.

Routing is by the spec's privacy class: cloud providers see only `public`
and `workspace` state; `personal` and `secret` state is answered locally or
not at all. Every call is logged. The client never acts on an answer; it
returns the answers plus a gate verdict and the caller's code decides.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Any

from bridge.decisions.log import DecisionLog, state_digest
from bridge.decisions.provider import DecisionError, DecisionProvider, providers_from_env
from bridge.decisions.spec import DecisionSpec, Verdict, evaluate_gate, load_specs
from bridge.decisions.types import Answers, Choice, Question

PROVIDERS_FOR_PRIVACY: dict[str, tuple[str, ...]] = {
    "public": ("typesafe", "local"),
    "workspace": ("typesafe", "local"),
    "personal": ("local",),
    "secret": (),
}


class DecisionUnavailable(DecisionError):
    """No provider is permitted or available for this spec's privacy class."""


@dataclass
class Decision:
    id: str
    spec: DecisionSpec
    answers: Answers
    verdict: Verdict

    @property
    def action(self) -> str:
        return self.verdict.action

    @property
    def effective_action(self) -> str:
        """What the caller may do with the verdict given the spec's rollout mode.

        shadow: log only, always `abstain` to the caller.
        advise: the verdict is a hint; the caller keeps its existing behavior.
        gate: the verdict may change behavior (subject to the caller's own
        escalate-only rule for anything irreversible).
        """
        if self.spec.mode == "shadow":
            return "abstain"
        return self.verdict.action


class DecisionClient:
    def __init__(
        self,
        providers: dict[str, DecisionProvider] | None = None,
        *,
        log: DecisionLog | None = None,
        specs: dict[str, DecisionSpec] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.providers = providers if providers is not None else providers_from_env()
        self.log = log if log is not None else DecisionLog()
        self.specs = specs if specs is not None else load_specs()
        self.rng = rng or random.Random()

    def spec(self, spec_id: str) -> DecisionSpec:
        try:
            return self.specs[spec_id]
        except KeyError as exc:
            raise KeyError(f"unknown decision spec {spec_id!r}") from exc

    def provider_for(self, spec: DecisionSpec) -> DecisionProvider:
        for name in PROVIDERS_FOR_PRIVACY.get(spec.privacy, ()):
            provider = self.providers.get(name)
            if provider is not None:
                return provider
        raise DecisionUnavailable(
            f"no provider permitted for privacy={spec.privacy!r} (have {sorted(self.providers)})"
        )

    def _prepare(
        self,
        spec: DecisionSpec,
        state: Any,
        options: dict[str, dict[str, Any]] | None,
    ) -> tuple[Any, dict[str, Question]]:
        questions: dict[str, Question] = dict(spec.questions)
        options = options or {}
        for key in spec.dynamic_options:
            if key not in options:
                raise ValueError(f"spec {spec.key} needs options for dynamic question {key!r}")
            questions[key] = Choice(spec.questions[key].instructions, dict(options[key]))
        for key, extra in options.items():
            if key not in spec.dynamic_options:
                raise ValueError(f"{key!r} is not a dynamic_options question of {spec.key}")
        if spec.shuffle_options:
            for key, q in list(questions.items()):
                if isinstance(q, Choice):
                    items = list(q.options.items())
                    self.rng.shuffle(items)
                    questions[key] = Choice(q.instructions, dict(items))
        if spec.frame:
            state = {"reader_guidance": spec.frame, "content": state}
        return state, questions

    def _finish(
        self, spec: DecisionSpec, provider: DecisionProvider, state: Any, answers: Answers,
        context: dict[str, Any] | None,
    ) -> Decision:
        verdict = evaluate_gate(spec, answers)
        decision = Decision(id=uuid.uuid4().hex[:12], spec=spec, answers=answers, verdict=verdict)
        self.log.append(
            {
                "kind": "decision",
                "decision_id": decision.id,
                "spec": spec.key,
                "mode": spec.mode,
                "privacy": spec.privacy,
                "provider": provider.name,
                "model": answers.model,
                "state_digest": state_digest(state),
                "answers": answers.compact(),
                "verdict": {"action": verdict.action, "score": verdict.score, "reasons": list(verdict.reasons)},
                "latency_ms": answers.latency_ms,
                "input_tokens": answers.input_tokens,
                "output_tokens": answers.output_tokens,
                "context": context or {},
            }
        )
        return decision

    async def decide(
        self,
        spec_id: str,
        state: Any,
        *,
        options: dict[str, dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Decision:
        spec = self.spec(spec_id)
        provider = self.provider_for(spec)
        prepared_state, questions = self._prepare(spec, state, options)
        answers = await provider.decide(prepared_state, questions, model=spec.model)
        return self._finish(spec, provider, prepared_state, answers, context)

    def decide_sync(
        self,
        spec_id: str,
        state: Any,
        *,
        options: dict[str, dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Decision:
        spec = self.spec(spec_id)
        provider = self.provider_for(spec)
        prepared_state, questions = self._prepare(spec, state, options)
        answers = provider.decide_sync(prepared_state, questions, model=spec.model)
        return self._finish(spec, provider, prepared_state, answers, context)
