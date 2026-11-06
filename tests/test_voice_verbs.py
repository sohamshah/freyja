"""VerbRegistry mechanics + the pinned `act` tool schema (contract §3)."""

import re

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


# All verbs the adapters register (slice 1 + slice-2 reach + slice-3
# native apps/Shortcuts + P2 files/clipboard + freyja.*); mission.spawn /
# mission.status / computer.do / freyja.ask are service-side. slack.* and
# web.read_page are temporarily disabled (GALDR-BUILD §12.3).
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
    "screen.look",
    "reminders.create",
    "reminders.list",
    "notes.append",
    "notes.create",
    "messages.send",
    "contacts.find",
    "calendar.today",
    "calendar.next",
    "mail.unread",
    "shortcuts.list",
    "shortcuts.run",
    "files.list",
    "files.open_latest",
    "files.reveal",
    "files.organize",
    "clipboard.read",
    "clipboard.write",
    "briefing.read",
    "freyja.sessions",
    "freyja.project_status",
}

# Confirm tier = outward/destructive actions: quitting an app, sending
# an iMessage someone else will read, or moving files on disk
# (files.organize). (slack.send was here too but slack.* is disabled.)
CONFIRM_VERBS = {"app.quit", "messages.send", "files.organize"}


def test_register_get_all_and_duplicate_raises():
    reg = VerbRegistry()
    a, b = _mk("demo.a"), _mk("demo.b")
    reg.register(a)
    reg.register(b)
    assert reg.get("demo.a") is a
    assert reg.get("demo.missing") is None
    assert reg.all() == [a, b]
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


def test_default_registry_verbs_tiers_catalog_and_schema_agree():
    """Cross-table invariant: every adapter verb is registered with the expected tier, has a catalog line and an enum entry, and only confirm-tier verbs carry the confirmation marker."""
    reg = build_default_registry()
    assert {v.name for v in reg.all()} == EXPECTED_VERBS
    for verb in reg.all():
        expected_tier = "confirm" if verb.name in CONFIRM_VERBS else "auto"
        assert verb.tier == expected_tier, verb.name
        assert verb.description
        assert callable(verb.run)
    catalog = reg.catalog_markdown()
    confirm_lines = [ln for ln in catalog.splitlines() if "[requires spoken confirmation]" in ln]
    assert {ln.split("(")[0] for ln in confirm_lines} == {f"- {v}" for v in CONFIRM_VERBS}
    assert len(catalog.splitlines()) == len(EXPECTED_VERBS)
    schema = reg.openai_tool_schema()
    assert set(schema["parameters"]["properties"]["verb"]["enum"]) == EXPECTED_VERBS


def test_tool_name_round_trip():
    reg = VerbRegistry()
    reg.register(_mk("freyja.project_status"))
    reg.register(_mk("spotify.play"))
    assert VerbRegistry.tool_name("freyja.project_status") == "freyja_project_status"
    assert reg.resolve_tool_name("freyja_project_status") == "freyja.project_status"
    assert reg.resolve_tool_name("spotify.play") == "spotify.play"  # dotted still resolves
    assert reg.resolve_tool_name("spotify_explode") is None


def test_responses_tools_one_per_verb_with_confirm_slot_and_exclude():
    reg = VerbRegistry()
    reg.register(_mk("spotify.play", params={"query": {"type": "string"}}, required=["query"]))
    reg.register(_mk("app.quit", params={"name": {"type": "string"}}, required=["name"], tier="confirm"))
    play, quit_ = reg.responses_tools()
    assert play == {
        "type": "function",
        "name": "spotify_play",
        "description": play["description"],
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    assert quit_["name"] == "app_quit"
    assert "confirm_token" in quit_["parameters"]["properties"]
    assert quit_["parameters"]["required"] == ["name"]  # token stays optional
    assert quit_["description"].endswith("Requires spoken confirmation.")
    # the registry's own params dict isn't mutated by the projection
    assert "confirm_token" not in reg.get("app.quit").params
    assert [t["name"] for t in reg.responses_tools(exclude=("spotify.play",))] == ["app_quit"]


def test_default_registry_responses_tools_names_are_valid():
    tools = build_default_registry().responses_tools()
    assert len(tools) == len({t["name"] for t in tools})
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["name"]) for t in tools)
