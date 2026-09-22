"""Tests for TwitterSearchTool."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from bridge.session_ledger import OBSERVATION_TOOLS, RESEARCH_TOOLS, SessionLedger
from bridge.tools.agent_types import AGENT_TYPES
from bridge.tools.registry import build_desktop_registry
from bridge.tools.summarize_context_tool import SummarizeContextTool
from bridge.tools.twitter_tool import (
    DEFAULT_XAI_MODEL,
    TwitterSearchTool,
    _resolve_api_key,
)


class TestTwitterTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = TwitterSearchTool()

    async def test_tool_definition(self):
        definition = self.tool.definition
        self.assertEqual(definition.name, "twitter_search")
        self.assertEqual(definition.tier.value, "hot")
        self.assertIn("query", definition.parameters["required"])
        props = definition.parameters["properties"]
        self.assertIn("query", props)
        self.assertIn("allowed_handles", props)
        self.assertIn("excluded_handles", props)
        self.assertIn("from_date", props)
        self.assertIn("to_date", props)
        self.assertIn("include_images", props)
        self.assertIn("include_videos", props)

    async def test_missing_query(self):
        result = await self.tool.execute("call_1", {})
        self.assertTrue(result.is_error)
        self.assertIn("query", result.content)

    async def test_query_coercion_from_tweet_url(self):
        with patch.object(self.tool, "_get_api_key", return_value="xai-test-key"):
            with patch.object(self.tool, "_get_client") as mock_get_client:
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Tweet summary content"}],
                        }
                    ]
                }
                mock_client.post.return_value = mock_resp
                mock_get_client.return_value = mock_client

                result = await self.tool.execute(
                    "call_2",
                    {"tweet_url": "https://x.com/jack/status/20"},
                )
                self.assertFalse(result.is_error)
                self.assertIn("Tweet summary content", result.content)

    async def test_query_coercion_from_handle(self):
        with patch.object(self.tool, "_get_api_key", return_value="xai-test-key"):
            with patch.object(self.tool, "_get_client") as mock_get_client:
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Handle content"}],
                        }
                    ]
                }
                mock_client.post.return_value = mock_resp
                mock_get_client.return_value = mock_client

                result = await self.tool.execute(
                    "call_handle",
                    {"query": "Latest posts", "handle": "@karpathy"},
                )
                self.assertFalse(result.is_error)
                # Verify payload had allowed_x_handles stripped of @
                call_kwargs = mock_client.post.call_args[1]
                tools_payload = call_kwargs["json"]["tools"]
                self.assertEqual(tools_payload[0]["allowed_x_handles"], ["karpathy"])

    async def test_mutual_exclusive_handles(self):
        result = await self.tool.execute(
            "call_3",
            {
                "query": "AI research",
                "allowed_handles": ["karpathy"],
                "excluded_handles": ["elonmusk"],
            },
        )
        self.assertTrue(result.is_error)
        self.assertIn("cannot specify both allowed_handles and excluded_handles", result.content)

    async def test_too_many_handles(self):
        result = await self.tool.execute(
            "call_4",
            {
                "query": "AI research",
                "allowed_handles": [f"user{i}" for i in range(11)],
            },
        )
        self.assertTrue(result.is_error)
        self.assertIn("allowed_handles cannot exceed 10", result.content)

    async def test_invalid_date_format(self):
        result = await self.tool.execute(
            "call_5",
            {
                "query": "AI research",
                "from_date": "2026/09/20",
            },
        )
        self.assertTrue(result.is_error)
        self.assertIn("from_date must be in YYYY-MM-DD format", result.content)

    async def test_missing_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("bridge.tools.twitter_tool._read_env_file_keys", return_value={}):
                result = await self.tool.execute("call_6", {"query": "test query"})
                self.assertTrue(result.is_error)
                self.assertIn("xAI API key not configured", result.content)

    async def test_groq_key_disambiguation_error(self):
        with patch.dict(os.environ, {"XAI_API_KEY": "gsk_123456789"}, clear=True):
            with patch("bridge.tools.twitter_tool._read_env_file_keys", return_value={}):
                result = await self.tool.execute("call_7", {"query": "test query"})
                self.assertTrue(result.is_error)
                self.assertIn("Found Groq API key", result.content)

    async def test_successful_search_with_annotations(self):
        with patch.object(self.tool, "_get_api_key", return_value="xai-valid-key"):
            with patch.object(self.tool, "_get_client") as mock_get_client:
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Discussion on LLM reasoning has surged.",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url": "https://x.com/researcher/status/100",
                                            "title": "Reasoning models overview",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
                mock_client.post.return_value = mock_resp
                mock_get_client.return_value = mock_client

                result = await self.tool.execute("call_8", {"query": "LLM reasoning"})
                self.assertFalse(result.is_error)
                self.assertIn("Discussion on LLM reasoning has surged.", result.content)
                self.assertIn("Reasoning models overview", result.content)
                self.assertIn("https://x.com/researcher/status/100", result.content)

    async def test_http_error_handling(self):
        with patch.object(self.tool, "_get_api_key", return_value="xai-valid-key"):
            with patch.object(self.tool, "_get_client") as mock_get_client:
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status_code = 429
                mock_resp.text = '{"error": "rate limit exceeded"}'
                mock_client.post.return_value = mock_resp
                mock_get_client.return_value = mock_client

                result = await self.tool.execute("call_9", {"query": "test"})
                self.assertTrue(result.is_error)
                self.assertIn("HTTP 429", result.content)

    async def test_timeout_handling(self):
        with patch.object(self.tool, "_get_api_key", return_value="xai-valid-key"):
            with patch.object(self.tool, "_get_client") as mock_get_client:
                mock_client = MagicMock()
                mock_client.post.side_effect = httpx.TimeoutException("timed out")
                mock_get_client.return_value = mock_client

                result = await self.tool.execute("call_10", {"query": "test"})
                self.assertTrue(result.is_error)
                self.assertIn("timed out", result.content)


class TestTwitterIntegration(unittest.TestCase):
    def test_key_resolver_prioritizes_xai_prefix(self):
        with patch.dict(os.environ, {"XAI_API_KEY": "gsk_groq", "GROK_API_KEY": "xai-real-key"}):
            key, err = _resolve_api_key()
            self.assertEqual(key, "xai-real-key")
            self.assertIsNone(err)

    def test_desktop_registry_wiring(self):
        registry = build_desktop_registry(workspace="/tmp")
        tool_names = [t.definition.name for t in registry._tools.values()]
        self.assertIn("twitter_search", tool_names)

    def test_session_ledger_tool_classification(self):
        self.assertIn("twitter_search", OBSERVATION_TOOLS)
        self.assertIn("twitter_search", RESEARCH_TOOLS)
        target = SessionLedger._observation_target("twitter_search", {"query": "xAI Grok 4"})
        self.assertEqual(target, "xAI Grok 4")

    def test_summarize_context_exploration_tools(self):
        self.assertIn("twitter_search", SummarizeContextTool.EXPLORATION_TOOLS)

    def test_subagent_tool_includes(self):
        self.assertIn("twitter_search", AGENT_TYPES["explore"].tool_include)
        self.assertIn("twitter_search", AGENT_TYPES["explore-fast"].tool_include)


if __name__ == "__main__":
    unittest.main()
