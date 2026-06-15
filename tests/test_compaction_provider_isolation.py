"""Compaction's working-memory extraction (Call B) runs inside a
ThreadPoolExecutor worker via asyncio.run, which spins up a fresh event loop and
closes it when done. If it reused the shared session provider's async client,
that client's connection pool would get bound to the throwaway loop and the next
real LLM call on the main loop would die with "RuntimeError: Event loop is
closed". Call B must run on an ISOLATED clone of the provider instead.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.compaction import SummaryCompaction


def _resp():
    return SimpleNamespace(
        data={"summary": "s", "actions_completed": ["a"], "entities": []},
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0
        ),
        model="fake-model",
        raw_text=None,
    )


class _CloneableProvider:
    """A provider with `_config` — the real providers all expose one, so Call B
    should clone it (type(provider)(_config)) and use the clone, never the
    shared instance."""

    instances: list = []

    def __init__(self, config=None):
        self._config = config if config is not None else {"k": "v"}
        self.name = "fake"
        self.model_id = "fake-model"
        self.used = False
        self.closed = False
        _CloneableProvider.instances.append(self)

    async def complete_structured(self, messages, **kw):
        self.used = True
        return _resp()

    async def close(self):
        self.closed = True


def test_extract_working_memory_uses_isolated_clone():
    _CloneableProvider.instances.clear()
    shared = _CloneableProvider(config={"k": "v"})

    out = SummaryCompaction()._extract_working_memory("a conversation to extract", shared)

    assert out is not None and out["summary"] == "s"
    # A fresh clone was constructed and used; the SHARED provider was not.
    assert len(_CloneableProvider.instances) == 2  # shared + clone
    assert shared.used is False, "shared provider's client must not be touched"
    clone = _CloneableProvider.instances[1]
    assert clone.used is True  # the clone did the call
    # The clone is closed within the throwaway loop (no aclose-on-closed-loop
    # noise / leak); the shared provider is never closed.
    assert clone.closed is True
    assert shared.closed is False


def test_extract_working_memory_passthrough_without_config():
    # A provider with no `_config` (e.g. a test mock) has no real loop-bound
    # client to poison, so it's used directly rather than cloned.
    class _NoConfigProvider:
        def __init__(self):
            self.name = "m"
            self.model_id = "m"
            self.used = False

        async def complete_structured(self, messages, **kw):
            self.used = True
            return _resp()

    p = _NoConfigProvider()
    out = SummaryCompaction()._extract_working_memory("conv", p)
    assert out is not None
    assert p.used is True


def test_extract_working_memory_skips_when_clone_fails():
    # If cloning raises, skip (return None) rather than risk poisoning the
    # shared client by falling back to it.
    class _UncloneableProvider:
        def __init__(self, config=None):
            if config is not None:
                raise RuntimeError("cannot clone")
            self._config = object()
            self.name = "u"
            self.model_id = "u"
            self.used = False

        async def complete_structured(self, messages, **kw):
            self.used = True
            return _resp()

    p = _UncloneableProvider()
    out = SummaryCompaction()._extract_working_memory("conv", p)
    assert out is None
    assert p.used is False  # shared instance never used
