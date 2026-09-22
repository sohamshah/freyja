"""
Twitter/X search tool using xAI's Grok API.

Uses the xAI Responses API with x_search tool to search Twitter/X posts,
conversations, user timelines, and discussions.
https://docs.x.ai/docs/tools

Async-native using executor for HTTP calls.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)

# xAI API configuration
XAI_API_BASE = "https://api.x.ai/v1"
DEFAULT_XAI_MODEL = "grok-4-1-fast-non-reasoning"
DEFAULT_TIMEOUT = 60.0

# Debug mode - set via environment variable
TWITTER_DEBUG = os.environ.get("TWITTER_DEBUG", "").lower() in ("1", "true", "yes")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _read_env_file_keys() -> dict[str, str]:
    """Read keys from ~/.freyja/.env and .env if present."""
    keys: dict[str, str] = {}
    for env_path in [Path.home() / ".freyja" / ".env", Path(".env")]:
        if env_path.is_file():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and v and k not in keys:
                        keys[k] = v
            except Exception:
                pass
    return keys


def _resolve_api_key(explicit_key: str | None = None) -> tuple[str | None, str | None]:
    """
    Resolve xAI API key from explicit arg, env, or ~/.freyja/.env.

    Handles disambiguation between Grok/xAI keys (typically starting with 'xai-')
    and Groq keys (starting with 'gsk_') which are sometimes mistakenly named
    XAI_API_KEY.

    Returns:
        (api_key, error_message)
    """
    if explicit_key and explicit_key.strip():
        return explicit_key.strip(), None

    env_file_keys = _read_env_file_keys()

    # Priority 1: Check if GROK_API_KEY is an xAI key
    grok_env = os.environ.get("GROK_API_KEY") or env_file_keys.get("GROK_API_KEY")
    if grok_env and grok_env.strip().startswith("xai-"):
        return grok_env.strip(), None

    # Priority 2: Check if XAI_API_KEY is an xAI key
    xai_env = os.environ.get("XAI_API_KEY") or env_file_keys.get("XAI_API_KEY")
    if xai_env and xai_env.strip().startswith("xai-"):
        return xai_env.strip(), None

    # Priority 3: Any non-Groq key in GROK_API_KEY
    if grok_env and grok_env.strip() and not grok_env.strip().startswith("gsk_"):
        return grok_env.strip(), None

    # Priority 4: Any non-Groq key in XAI_API_KEY
    if xai_env and xai_env.strip() and not xai_env.strip().startswith("gsk_"):
        return xai_env.strip(), None

    # Check if a Groq key was mistakenly configured as XAI_API_KEY
    has_groq_key = (
        (xai_env and xai_env.strip().startswith("gsk_"))
        or (grok_env and grok_env.strip().startswith("gsk_"))
    )
    if has_groq_key:
        return None, (
            "Found Groq API key (starts with 'gsk_') configured in XAI_API_KEY, but xAI Grok "
            "requires a key starting with 'xai-'. Please configure GROK_API_KEY or XAI_API_KEY "
            "with a valid key from https://console.x.ai."
        )

    return None, (
        "xAI API key not configured. Set GROK_API_KEY or XAI_API_KEY in environment or ~/.freyja/.env."
    )


class TwitterSearchTool:
    """
    Search Twitter/X using xAI's Grok API with x_search tool.

    Enables keyword search, semantic search, user handle filtering, date ranges,
    and fetching tweet summaries and discussions using Grok's native X search.
    """

    def __init__(self, api_key: str | None = None, tier: ToolTier = ToolTier.HOT):
        self._api_key = api_key
        self._tier = tier
        self._client: httpx.Client | None = None

    def _get_api_key(self) -> str:
        key, err = _resolve_api_key(self._api_key)
        if not key:
            raise ValueError(err or "xAI API key not configured.")
        return key

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)
        return self._client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="twitter_search",
            summary="Search Twitter/X for posts, discussions, and users",
            tier=self._tier,
            description="""Search Twitter/X for posts, user timelines, discussions, and topics using Grok's native X search.

CRITICAL: Craft expert-level natural language queries that anticipate what the user actually wants to know.

Think like a domain expert: What would someone knowledgeable about this topic really want to understand? What are the interesting angles, controversies, comparisons, or insights that matter?

Example - User asks: "what are people saying about OpenClaw"
BAD query: "openclaw" (keyword-style, misses context)
BAD query: "What are people saying about OpenClaw?" (too literal, shallow)
GOOD query: "What is the developer sentiment around OpenClaw as an AI agent framework? What are the main use cases people are excited about, what limitations or frustrations are being discussed, and how does it compare to alternatives like AutoGPT or CrewAI?"

Example - User asks: "karpathy's recent posts"
BAD query: "karpathy" (keyword)
BAD query: "What has Karpathy posted recently?" (shallow)
GOOD query: "What technical insights, AI industry commentary, or product recommendations has Andrej Karpathy shared recently? What projects is he working on or excited about, and what predictions or hot takes has he made about the future of AI?"

Example - Reading a specific tweet or thread:
GOOD query: "Read and summarize this tweet and its key replies: https://x.com/username/status/123456789"

Parameters:
- query: Expert-crafted natural language question, topic, or tweet URL (required)
- allowed_handles: Only search posts from these X handles (max 10, without @ symbol)
- excluded_handles: Exclude posts from these X handles (max 10, without @ symbol)
- from_date: Start date (YYYY-MM-DD format)
- to_date: End date (YYYY-MM-DD format)
- include_images: Analyze images in posts (default: false)
- include_videos: Analyze videos in posts (default: false)

Returns synthesized insights from X posts with citations to source tweets.""",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query - question, topic, or tweet URL to search on X/Twitter",
                    },
                    "allowed_handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only include posts from these X handles (max 10, without @ symbol)",
                    },
                    "excluded_handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exclude posts from these X handles (max 10, without @ symbol)",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format",
                    },
                    "include_images": {
                        "type": "boolean",
                        "description": "Enable analysis of images in posts (default: false)",
                    },
                    "include_videos": {
                        "type": "boolean",
                        "description": "Enable analysis of videos in posts (default: false)",
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute Twitter/X search asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Synchronous implementation executed in background thread."""
        query = str(arguments.get("query") or "").strip()

        # Resilient argument coercion
        if not query:
            tweet_url = str(arguments.get("tweet_url") or arguments.get("url") or "").strip()
            if tweet_url:
                query = f"Read and summarize this tweet and notable discussion: {tweet_url}"

        if not query:
            return ToolResult(
                call_id=call_id,
                content="Error: 'query' parameter is required.",
                is_error=True,
            )

        allowed_handles = arguments.get("allowed_handles")
        excluded_handles = arguments.get("excluded_handles")

        # Allow passing handle / username as string shorthand
        if not allowed_handles:
            handle = arguments.get("handle") or arguments.get("username")
            if handle and isinstance(handle, str):
                allowed_handles = [handle.strip()]

        from_date = arguments.get("from_date")
        to_date = arguments.get("to_date")
        include_images = bool(arguments.get("include_images", False))
        include_videos = bool(arguments.get("include_videos", False))

        # Validate handle constraints
        if allowed_handles and excluded_handles:
            return ToolResult(
                call_id=call_id,
                content="Error: cannot specify both allowed_handles and excluded_handles",
                is_error=True,
            )

        if allowed_handles:
            if not isinstance(allowed_handles, (list, tuple)):
                return ToolResult(
                    call_id=call_id,
                    content="Error: allowed_handles must be an array of strings",
                    is_error=True,
                )
            if len(allowed_handles) > 10:
                return ToolResult(
                    call_id=call_id,
                    content="Error: allowed_handles cannot exceed 10 handles",
                    is_error=True,
                )

        if excluded_handles:
            if not isinstance(excluded_handles, (list, tuple)):
                return ToolResult(
                    call_id=call_id,
                    content="Error: excluded_handles must be an array of strings",
                    is_error=True,
                )
            if len(excluded_handles) > 10:
                return ToolResult(
                    call_id=call_id,
                    content="Error: excluded_handles cannot exceed 10 handles",
                    is_error=True,
                )

        # Validate dates if provided
        if from_date and not _DATE_RE.match(str(from_date)):
            return ToolResult(
                call_id=call_id,
                content="Error: from_date must be in YYYY-MM-DD format",
                is_error=True,
            )

        if to_date and not _DATE_RE.match(str(to_date)):
            return ToolResult(
                call_id=call_id,
                content="Error: to_date must be in YYYY-MM-DD format",
                is_error=True,
            )

        try:
            api_key = self._get_api_key()
        except ValueError as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: {e}",
                is_error=True,
            )

        # Build x_search tool configuration
        x_search_tool: dict[str, Any] = {"type": "x_search"}

        if allowed_handles:
            x_search_tool["allowed_x_handles"] = [
                str(h).lstrip("@").strip() for h in allowed_handles if str(h).strip()
            ]

        if excluded_handles:
            x_search_tool["excluded_x_handles"] = [
                str(h).lstrip("@").strip() for h in excluded_handles if str(h).strip()
            ]

        if from_date:
            x_search_tool["from_date"] = str(from_date)

        if to_date:
            x_search_tool["to_date"] = str(to_date)

        if include_images:
            x_search_tool["enable_image_understanding"] = True

        if include_videos:
            x_search_tool["enable_video_understanding"] = True

        model = os.environ.get("XAI_MODEL") or DEFAULT_XAI_MODEL

        payload = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": query,
                }
            ],
            "tools": [x_search_tool],
        }

        if TWITTER_DEBUG:
            logger.info("xAI Twitter Search request: %s", json.dumps(payload, default=str))

        try:
            client = self._get_client()
            response = client.post(
                f"{XAI_API_BASE}/responses",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=payload,
            )

            if response.status_code != 200:
                error_text = response.text
                return ToolResult(
                    call_id=call_id,
                    content=f"Error: xAI API returned HTTP {response.status_code}: {error_text}",
                    is_error=True,
                )

            result = response.json()

            if TWITTER_DEBUG:
                logger.info("xAI Twitter Search response: %s", json.dumps(result, default=str))

        except httpx.TimeoutException:
            return ToolResult(
                call_id=call_id,
                content="Error: Request to xAI API timed out after 60 seconds",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: Request to xAI API failed: {e}",
                is_error=True,
            )

        return self._format_response(call_id, query, result)

    def _format_response(
        self, call_id: str, query: str, result: dict[str, Any]
    ) -> ToolResult:
        """Format the xAI response and citations into a clean readable result."""
        output_lines = [
            f"X/Twitter Search: {query}",
            "-" * 60,
        ]

        output_text = result.get("output_text", "")
        collected_citations: list[dict[str, str]] = []

        # Extract output text and citations from the 'output' array
        output_items = result.get("output", [])
        if isinstance(output_items, list):
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "message" or item.get("role") == "assistant":
                    content_blocks = item.get("content", [])
                    if isinstance(content_blocks, list):
                        for block in content_blocks:
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type")
                            if block_type in ("output_text", "text") and not output_text:
                                output_text = block.get("text", "")

                            # Extract annotations (url_citation)
                            annotations = block.get("annotations", [])
                            if isinstance(annotations, list):
                                for annot in annotations:
                                    if isinstance(annot, dict) and annot.get("url"):
                                        collected_citations.append({
                                            "url": annot.get("url", ""),
                                            "title": str(annot.get("title") or annot.get("url", "")),
                                        })

        # Also check top-level citations field if present
        top_citations = result.get("citations", [])
        if isinstance(top_citations, list):
            for cit in top_citations:
                if isinstance(cit, dict) and cit.get("url"):
                    collected_citations.append({
                        "url": cit.get("url", ""),
                        "title": str(cit.get("title") or cit.get("url", "")),
                    })
                elif isinstance(cit, str) and cit:
                    collected_citations.append({"url": cit, "title": cit})

        if output_text and output_text.strip():
            output_lines.append("")
            output_lines.append(output_text.strip())
        else:
            output_lines.append("")
            output_lines.append("No results found on X/Twitter for this query.")

        # Deduplicate citations while preserving order
        seen_urls: set[str] = set()
        unique_citations: list[dict[str, str]] = []
        for cit in collected_citations:
            url = cit.get("url", "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_citations.append(cit)

        if unique_citations:
            output_lines.append("")
            output_lines.append("-" * 60)
            output_lines.append("Citations:")
            for i, citation in enumerate(unique_citations[:15], 1):
                url = citation.get("url", "")
                title = citation.get("title", "")
                if title and title != url and not title.isdigit():
                    output_lines.append(f"  [{i}] {title}")
                    output_lines.append(f"      {url}")
                else:
                    output_lines.append(f"  [{i}] {url}")

        return ToolResult(
            call_id=call_id,
            content="\n".join(output_lines),
            is_error=False,
        )
