"""A provider's sync `complete()` must declare every parameter its body uses.

`FireworksProvider.complete()` forwarded `thinking=thinking` to `_build_request`
while its own signature never declared `thinking` — a latent NameError that only
fires at call time, so imports and type checks both stay green.

It went unnoticed because compaction calls `complete()` twice on purpose:

    try:
        provider.complete(**kwargs, thinking=ThinkingConfig(enabled=False))
    except TypeError:
        provider.complete(**kwargs)          # <- non-Anthropic providers

The first call raises TypeError (unexpected kwarg) and is swallowed by the
fallback, which is exactly the shape the fallback exists to handle. The second
call then enters the body and dies with NameError — a different exception class,
so nothing catches it, and the summary silently fails. Compaction reported
success with tokens_before == tokens_after and the transcript never shrank.

Both tests below are needed: the static one covers every provider at once
(including ones added later), the behavioural one pins the exact call shape
compaction uses.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ENGINE = pathlib.Path(__file__).resolve().parent.parent / "engine"

# Methods that take an optional per-call reasoning override.
_COMPLETION_METHODS = ("complete", "complete_async", "stream", "stream_to_response")


def _provider_methods():
    """Yield (path, class, funcdef) for each completion method in each provider."""
    for path in sorted(ENGINE.glob("*_provider.py")):
        tree = ast.parse(path.read_text())
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for fn in cls.body:
                if (
                    isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.name in _COMPLETION_METHODS
                ):
                    yield path.name, cls.name, fn


def _declared(fn) -> set[str]:
    a = fn.args
    return {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)} | {
        p.arg for p in (a.vararg, a.kwarg) if p
    }


def _assigned(fn) -> set[str]:
    """Names bound inside the body — locals, walrus, with/except/for targets."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
    return out


@pytest.mark.parametrize(
    "filename,classname,fn",
    [(f, c, fn) for f, c, fn in _provider_methods()],
    ids=lambda v: v if isinstance(v, str) else getattr(v, "name", "fn"),
)
def test_completion_method_declares_the_reasoning_param_it_forwards(
    filename, classname, fn
):
    """If the body reads `thinking`, the signature must bind it."""
    reads = {
        n.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    if "thinking" not in reads:
        return  # provider bakes reasoning in at construction time — fine.

    bound = _declared(fn) | _assigned(fn)
    assert "thinking" in bound, (
        f"{filename}:{classname}.{fn.name} forwards `thinking` but never binds it — "
        "this raises NameError at call time, not import time."
    )


def test_fireworks_complete_accepts_the_kwargs_compaction_passes(monkeypatch):
    """The exact two-call shape from engine/compaction.py:1036-1046."""
    import openai

    from engine.fireworks_provider import FireworksProvider
    from engine.types import Message, ThinkingConfig

    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: object())

    provider = FireworksProvider()

    seen: dict = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return {"model": "stub"}

    monkeypatch.setattr(provider, "_build_request", _capture)
    monkeypatch.setattr(provider, "_parse_response", lambda r: r)

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return "response"

    provider._client = _Client()

    kwargs = {"messages": [Message(role="user", content="summarize")], "max_tokens": 32000}

    # Call 1 — with the override. Must NOT raise TypeError now that the
    # parameter exists, and the value must reach _build_request rather than
    # being silently dropped.
    provider.complete(**kwargs, thinking=ThinkingConfig(enabled=False, effort="none"))
    assert seen["thinking"] is not None
    assert seen["thinking"].enabled is False

    # Call 2 — the compaction fallback path, no override. This is the call that
    # used to raise NameError.
    seen.clear()
    provider.complete(**kwargs)
    assert seen["thinking"] is None
