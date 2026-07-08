"""VerbRegistry mechanics + the pinned `act` tool schema (contract §3)."""

import pytest

from bridge.voice.verbs import Verb, VerbRegistry, VerbResult, build_default_registry


async def _noop(args):
    return VerbResult(ok=True, summary="ok")


def _mk(name, tier="auto", params=None, required=None, description="does a thing"):
    return Verb(
        name=name,
        description=description,
        params=params or {},
        required=required or [],
        tier=tier,
        run=_noop,
    )


# All slice-1 verbs the adapters register; mission.spawn is service-side.
EXPECTED_VERBS = {
    "spotify.play",
    "spotify.pause",
    "spotify.resume",
    "spotify.next",
    "spotify.previous",
    "spotify.now_playing",
    "system.volume",
    "app.open",
    "app.focus",
    "app.quit",
    "app.frontmost",
    "timer.set",
    "timer.list",
    "timer.cancel",
}


def test_register_get_all():
    reg = VerbRegistry()
    a, b = _mk("demo.a"), _mk("demo.b")
    reg.register(a)
    reg.register(b)
    assert reg.get("demo.a") is a
    assert reg.get("demo.missing") is None
    assert reg.all() == [a, b]


def test_register_duplicate_raises():
    reg = VerbRegistry()
    reg.register(_mk("demo.a"))
    with pytest.raises(ValueError):
        reg.register(_mk("demo.a"))


def test_catalog_markdown_format():
    reg = VerbRegistry()
    reg.register(
        _mk(
            "demo.play",
            params={"query": {"type": "string"}, "level": {"type": "integer"}},
            required=["query"],
            description="play something",
        )
    )
    reg.register(_mk("demo.quit", tier="confirm", description="quit it"))
    assert reg.catalog_markdown().splitlines() == [
        "- demo.play(query: string, level?: integer) — play something",
        "- demo.quit() — quit it [requires spoken confirmation]",
    ]


def test_openai_tool_schema_exact_shape():
    reg = VerbRegistry()
    reg.register(_mk("demo.a"))
    reg.register(_mk("demo.b"))
    schema = reg.openai_tool_schema()
    assert schema == {
        "type": "function",
        "name": "act",
        "description": (
            "Execute a Mac action. Pick verb from the catalog in your "
            "instructions; put its arguments in args."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verb": {"type": "string", "enum": ["demo.a", "demo.b"]},
                "args": {"type": "object"},
                "confirm_token": {"type": "string"},
            },
            "required": ["verb"],
        },
    }


def test_default_registry_verbs_and_tiers():
    reg = build_default_registry()
    assert {v.name for v in reg.all()} == EXPECTED_VERBS
    for verb in reg.all():
        expected_tier = "confirm" if verb.name == "app.quit" else "auto"
        assert verb.tier == expected_tier, verb.name
        assert verb.description
        assert callable(verb.run)


def test_default_registry_catalog_confirm_marker():
    catalog = build_default_registry().catalog_markdown()
    confirm_lines = [ln for ln in catalog.splitlines() if "[requires spoken confirmation]" in ln]
    assert len(confirm_lines) == 1
    assert confirm_lines[0].startswith("- app.quit(")
    # Every registered verb has a line.
    assert len(catalog.splitlines()) == len(EXPECTED_VERBS)


def test_default_registry_tool_schema_enum():
    schema = build_default_registry().openai_tool_schema()
    assert set(schema["parameters"]["properties"]["verb"]["enum"]) == EXPECTED_VERBS


def test_verb_result_defaults_are_independent():
    r1, r2 = VerbResult(ok=True, summary="a"), VerbResult(ok=False, summary="b")
    assert r1.say is None and r1.undo is None and r1.error is None
    r1.data["k"] = "v"
    assert r2.data == {}  # default_factory: no shared mutable state
