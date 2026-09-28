"""Decision providers: anything that answers a typed question battery.

`TypeSafeProvider` speaks the System One wire format
(`POST {base_url}/v1/systemone`). The same class serves a local
OpenDecision-style endpoint by pointing `base_url` elsewhere, which is how
personal/secret state stays on the machine.
"""

from __future__ import annotations

import os
import time
from typing import Any, Protocol

import httpx

from bridge.decisions.types import Answers, Question, parse_answers, question_payload

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
ENV_API_KEY = "TYPESAFE_AI_API_KEY"
ENV_BASE_URL = "TYPESAFE_AI_BASE_URL"
ENV_LOCAL_BASE_URL = "DECISIONS_LOCAL_BASE_URL"


class DecisionError(RuntimeError):
    """The provider could not produce answers (network, auth, schema)."""


class DecisionProvider(Protocol):
    name: str

    async def decide(
        self, state: Any, questions: dict[str, Question], *, model: str | None = None
    ) -> Answers: ...

    def decide_sync(
        self, state: Any, questions: dict[str, Question], *, model: str | None = None
    ) -> Answers: ...


class TypeSafeProvider:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        name: str = "typesafe",
        timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get(ENV_API_KEY) or ""
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key) or self.base_url != DEFAULT_BASE_URL

    def _request(
        self, state: Any, questions: dict[str, Question], model: str | None
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        if not questions:
            raise DecisionError("no questions")
        body = {
            "model": model or self.model,
            "state": state,
            "questions": {k: question_payload(q) for k, q in questions.items()},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return f"{self.base_url}/v1/systemone", headers, body

    async def decide(
        self, state: Any, questions: dict[str, Question], *, model: str | None = None
    ) -> Answers:
        url, headers, body = self._request(state, questions, model)
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise DecisionError(f"{self.name}: transport error: {exc}") from exc
        return self._finish(resp, t0)

    def decide_sync(
        self, state: Any, questions: dict[str, Question], *, model: str | None = None
    ) -> Answers:
        url, headers, body = self._request(state, questions, model)
        t0 = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise DecisionError(f"{self.name}: transport error: {exc}") from exc
        return self._finish(resp, t0)

    def _finish(self, resp: httpx.Response, t0: float) -> Answers:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code != 200:
            raise DecisionError(
                f"{self.name}: HTTP {resp.status_code}: {resp.text[:400]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DecisionError(f"{self.name}: non-JSON response") from exc
        return parse_answers(payload, provider=self.name, latency_ms=latency_ms)


def providers_from_env() -> dict[str, DecisionProvider]:
    """Build the provider set from environment variables.

    `TYPESAFE_AI_API_KEY` enables the cloud provider. `DECISIONS_LOCAL_BASE_URL`
    enables a local System-One-compatible endpoint under the name `local`.
    """
    out: dict[str, DecisionProvider] = {}
    cloud = TypeSafeProvider()
    if cloud.api_key:
        out["typesafe"] = cloud
    local_url = os.environ.get(ENV_LOCAL_BASE_URL)
    if local_url:
        out["local"] = TypeSafeProvider(api_key="", base_url=local_url, name="local")
    return out
