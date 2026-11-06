"""TwitterSearchTool: argument validation, xAI key resolution, and the Responses payload in and out."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from bridge.tools.twitter_tool import TwitterSearchTool, _resolve_api_key


def _client(payload=None, *, status: int = 200, text: str = "", side_effect=None) -> MagicMock:
    client = MagicMock()
    if side_effect is not None:
        client.post.side_effect = side_effect
        return client
    response = MagicMock()
    response.status_code = status
    response.text = text
    if payload is not None:
        response.json.return_value = payload
    client.post.return_value = response
    return client


def _message(text: str, annotations=None) -> dict:
    block = {"type": "output_text", "text": text}
    if annotations:
        block["annotations"] = annotations
    return {"output": [{"type": "message", "content": [block]}]}


async def _run(tool, arguments, client):
    with (
        patch.object(tool, "_get_api_key", return_value="xai-test-key"),
        patch.object(tool, "_get_client", return_value=client),
    ):
        return await tool.execute("call", arguments)


@pytest.mark.parametrize(
    ("arguments", "complaint"),
    [
        ({}, "query"),
        (
            {"query": "AI research", "allowed_handles": ["karpathy"], "excluded_handles": ["elonmusk"]},
            "cannot specify both allowed_handles and excluded_handles",
        ),
        (
            {"query": "AI research", "allowed_handles": [f"user{i}" for i in range(11)]},
            "allowed_handles cannot exceed 10",
        ),
        ({"query": "AI research", "from_date": "2026/09/20"}, "from_date must be in YYYY-MM-DD format"),
    ],
)
async def test_bad_arguments_are_refused_before_any_request(arguments, complaint):
    client = _client(_message("unused"))
    result = await _run(TwitterSearchTool(), arguments, client)
    assert result.is_error
    assert complaint in result.content
    client.post.assert_not_called()


async def test_a_tweet_url_or_handle_becomes_a_query_and_handles_lose_their_at():
    tool = TwitterSearchTool()
    client = _client(_message("Tweet summary content"))
    result = await _run(tool, {"tweet_url": "https://x.com/jack/status/20"}, client)
    assert not result.is_error
    assert "Tweet summary content" in result.content

    client = _client(_message("Handle content"))
    result = await _run(tool, {"query": "Latest posts", "handle": "@karpathy"}, client)
    assert not result.is_error
    tools_payload = client.post.call_args[1]["json"]["tools"]
    assert tools_payload[0]["allowed_x_handles"] == ["karpathy"]


async def test_missing_or_groq_key_is_a_clear_error_and_an_xai_key_wins():
    tool = TwitterSearchTool()
    no_env_file = patch("bridge.tools.twitter_tool._read_env_file_keys", return_value={})
    with patch.dict(os.environ, {}, clear=True), no_env_file:
        result = await tool.execute("call", {"query": "test query"})
    assert result.is_error
    assert "xAI API key not configured" in result.content

    # A Groq key pasted into XAI_API_KEY is named as such, not sent to xAI.
    with patch.dict(os.environ, {"XAI_API_KEY": "gsk_123456789"}, clear=True), no_env_file:
        result = await tool.execute("call", {"query": "test query"})
    assert result.is_error
    assert "Found Groq API key" in result.content

    with patch.dict(os.environ, {"XAI_API_KEY": "gsk_groq", "GROK_API_KEY": "xai-real-key"}):
        assert _resolve_api_key() == ("xai-real-key", None)


async def test_search_result_carries_the_text_and_its_citations():
    client = _client(
        _message(
            "Discussion on LLM reasoning has surged.",
            annotations=[
                {
                    "type": "url_citation",
                    "url": "https://x.com/researcher/status/100",
                    "title": "Reasoning models overview",
                }
            ],
        )
    )
    result = await _run(TwitterSearchTool(), {"query": "LLM reasoning"}, client)
    assert not result.is_error
    assert "Discussion on LLM reasoning has surged." in result.content
    assert "Reasoning models overview" in result.content
    assert "https://x.com/researcher/status/100" in result.content


async def test_http_errors_and_timeouts_are_reported_not_raised():
    tool = TwitterSearchTool()
    result = await _run(tool, {"query": "test"}, _client(status=429, text='{"error": "rate limit exceeded"}'))
    assert result.is_error
    assert "HTTP 429" in result.content

    result = await _run(tool, {"query": "test"}, _client(side_effect=httpx.TimeoutException("timed out")))
    assert result.is_error
    assert "timed out" in result.content
