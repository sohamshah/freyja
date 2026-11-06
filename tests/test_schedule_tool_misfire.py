"""The schedule tool tells the model what misfire_policy does.

The tool's schema listed misfire_policy as a bare enum, and its
description never named it, so the model could not know that the value
decides whether a missed reminder still runs, or when to pick which.
"""

from __future__ import annotations

from bridge.tools.schedule_tool import _DESCRIPTION, _PARAMETERS


def test_the_schema_explains_each_misfire_policy() -> None:
    field = _PARAMETERS["properties"]["misfire_policy"]

    assert field["enum"] == ["skip", "fire_once", "fire_all"]
    text = field.get("description", "")
    for value in field["enum"]:
        assert f"{value}:" in text, value
    assert "one-time" in text and "recurring" in text


def test_the_description_says_when_to_use_each_misfire_policy() -> None:
    assert "misfire_policy" in _DESCRIPTION
    for value in ("skip", "fire_once", "fire_all"):
        assert f'"{value}"' in _DESCRIPTION, value
