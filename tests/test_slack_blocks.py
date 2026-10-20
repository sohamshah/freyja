"""Block Kit builders: shapes Slack accepts and the limits it enforces."""

from __future__ import annotations

import json

import pytest

from bridge.gateway import slack_blocks as b


def test_feedback_block_carries_session_and_turn_in_both_values():
    block = b.feedback_block("freyja:slack:T:dm:D", "1.2")
    assert block["type"] == "context_actions"
    el = block["elements"][0]
    assert el["type"] == "feedback_buttons" and el["action_id"] == b.FEEDBACK_ACTION_ID
    pos = b.parse_feedback_value(el["positive_button"]["value"])
    neg = b.parse_feedback_value(el["negative_button"]["value"])
    assert pos == {"session_key": "freyja:slack:T:dm:D", "turn_ts": "1.2", "verdict": "+"}
    assert neg["verdict"] == "-"
    assert len(el["positive_button"]["value"]) <= 2000


def test_feedback_value_survives_a_very_long_key():
    value = b.feedback_value("k" * 5000, "1.0", "+")
    assert len(value) <= 2000
    assert b.parse_feedback_value(value)["verdict"] == "+"


def test_data_table_keeps_numbers_numeric_and_pads_short_rows():
    block = b.data_table_block(["Repo", "PRs"], [["freyja", 3], ["evals"]], caption="Open PRs", page_size=10)
    assert block["type"] == "data_table" and block["page_size"] == 10
    header, r1, r2 = block["rows"]
    assert [c["text"] for c in header] == ["Repo", "PRs"]
    assert r1[1] == {"type": "raw_number", "value": 3, "text": "3"}
    assert r2[1] == {"type": "raw_text", "text": ""}


@pytest.mark.parametrize(
    "columns,rows,msg",
    [
        ([], [[1]], "column"),
        (["a"] * 21, [[1] * 21], "20"),
        (["a"], [], "row"),
        (["a"], [[1]] * 201, "200"),
    ],
)
def test_data_table_limits(columns, rows, msg):
    with pytest.raises(ValueError, match=msg):
        b.data_table_block(columns, rows)


def test_chart_bar_builds_categories_from_the_first_series():
    block = b.chart_block(
        "bar", "Turns per day",
        series=[{"name": "Turns", "data": [{"label": "Mon", "value": 4}, {"label": "Tue", "value": 7}]}],
        x_label="Day", y_label="Turns",
    )
    assert block["chart"]["type"] == "bar"
    assert block["chart"]["axis_config"]["categories"] == ["Mon", "Tue"]
    assert block["chart"]["series"][0]["data"][1] == {"label": "Tue", "value": 7.0}


def test_chart_pie_rejects_zero_segments_and_long_labels():
    with pytest.raises(ValueError, match="greater than zero"):
        b.chart_block("pie", "Share", segments=[{"label": "a", "value": 0}])
    with pytest.raises(ValueError, match="20 characters"):
        b.chart_block("pie", "Share", segments=[{"label": "x" * 21, "value": 1}])
    with pytest.raises(ValueError, match="kind"):
        b.chart_block("scatter", "Share", segments=[{"label": "a", "value": 1}])


def test_cards_become_a_carousel_when_several():
    one = b.card_blocks([{"title": "PR 12", "body": "ready", "links": [{"text": "Open", "url": "https://x/1"}]}])
    assert one[0]["type"] == "card" and one[0]["actions"][0]["url"] == "https://x/1"
    many = b.card_blocks([{"title": f"c{i}"} for i in range(3)])
    assert many[0]["type"] == "carousel" and len(many[0]["cards"]) == 3
    with pytest.raises(ValueError, match="10"):
        b.card_blocks([{"title": "x"}] * 11)


def test_card_body_is_cut_to_slack_limit():
    card = b.card_block({"title": "t", "body": "y" * 500})
    assert len(card["body"]["text"]) <= 200 and card["body"]["text"].endswith("…")


def test_container_and_markdown_and_video_shapes():
    c = b.container_block("Checks", [b.section_block("*ok*")], subtitle="3 of 3", collapsed=True)
    assert c["is_collapsible"] and c["default_collapsed"] and c["child_blocks"][0]["type"] == "section"
    with pytest.raises(ValueError):
        b.container_block("x", [])
    assert b.markdown_block("| a |\n|---|\n| 1 |")["type"] == "markdown"
    with pytest.raises(ValueError, match="12000"):
        b.markdown_block("x" * 12_001)
    v = b.video_block("https://v/e", title="Demo", thumbnail_url="https://v/t.png")
    assert v["type"] == "video" and v["title"]["text"] == "Demo"
    with pytest.raises(ValueError, match="https"):
        b.video_block("http://v/e", title="Demo", thumbnail_url="https://v/t.png")


def test_home_view_stays_under_the_block_cap_and_lists_rows():
    view = b.home_view(
        model="claude-opus-5-5", mode="bus",
        sessions=[{"title": f"s{i}", "detail": "idle"} for i in range(40)],
        jobs=[{"title": "briefing", "detail": "daily"}],
        candidates=[],
        generated_at="09:00",
    )
    assert view["type"] == "home" and len(view["blocks"]) <= 100
    text = json.dumps(view)
    assert "briefing" in text and "Nothing to review" in text and "s11" in text and "s12" not in text


def test_suggested_prompts_cap_at_four_and_drop_empties():
    out = b.suggested_prompts([("a", "A"), ("", "B"), ("c", "C"), ("d", "D"), ("e", "E"), ("f", "F")])
    assert [p["title"] for p in out] == ["a", "c", "d", "e"]
