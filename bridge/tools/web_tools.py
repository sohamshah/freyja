"""
Web tools for the CLI agent.

Uses Parallel Web Systems SDK for web search and content extraction.
https://parallel.ai/docs/

All tools are async-native, using executor for SDK calls.
"""

from __future__ import annotations

import asyncio
import functools
import os
from typing import Any

# Timeout for web operations — prevents subagents from hanging forever
# on unresponsive URLs. 30s is generous for any real page.
WEB_FETCH_TIMEOUT = 30
WEB_SEARCH_TIMEOUT = 20

try:
    from parallel import Parallel  # type: ignore
except Exception:  # pragma: no cover - optional dep
    Parallel = None  # type: ignore[assignment]

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier


def _get_parallel_api_key() -> str | None:
    """Read PARALLEL_API_KEY from env (no settings dependency)."""
    return os.environ.get("PARALLEL_API_KEY")


class WebSearchTool:
    """
    Search the web using Parallel Search API.

    Returns relevant URLs with excerpts optimized for LLM consumption.
    """

    def __init__(self, api_key: str | None = None):
        """
        Initialize with optional API key.

        If not provided, uses PARALLEL_API_KEY from environment/settings.
        """
        self._api_key = api_key
        self._client: Parallel | None = None

    def _get_client(self):
        if self._client:
            return self._client

        api_key = self._api_key or _get_parallel_api_key()
        if not api_key:
            raise ValueError("PARALLEL_API_KEY not configured")

        self._client = Parallel(api_key=api_key)
        return self._client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web_search",
            summary="Search the web",
            tier=ToolTier.HOT,
            description="""Search the web for information.

Uses Parallel's AI-optimized search API to find relevant web pages.
Returns URLs with extended excerpts suitable for LLM processing.

Best practices:
- Be specific in your search objective
- Use search queries for keyword-based searches
- Combine objective + queries for best results

Returns ranked results with:
- URL and title
- Relevance score
- Extended excerpt from the page""",
            parameters={
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "description": "Natural language description of what you're looking for",
                    },
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional search queries (keywords) to use",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 10, max: 20)",
                    },
                },
                "required": ["objective"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute web search asynchronously with timeout."""
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    functools.partial(self._execute_sync, call_id, arguments),
                ),
                timeout=WEB_SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                call_id=call_id,
                content=f"Error: web search timed out after {WEB_SEARCH_TIMEOUT}s",
                is_error=True,
            )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        objective = arguments.get("objective", "")
        queries = arguments.get("queries", [])
        max_results = min(arguments.get("max_results", 10), 20)

        if not objective:
            return ToolResult(
                call_id=call_id,
                content="Error: objective is required",
                is_error=True,
            )

        try:
            client = self._get_client()
        except ValueError as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: {e}",
                is_error=True,
            )

        try:
            # Use the SDK's beta.search method
            search_result = client.beta.search(
                objective=objective,
                search_queries=queries if queries else None,
                max_results=max_results,
                excerpts={"max_chars_per_result": 10000},
            )

            results = search_result.results if hasattr(search_result, 'results') else []

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: Search request failed: {e}",
                is_error=True,
            )

        if not results:
            return ToolResult(
                call_id=call_id,
                content=f"No results found for: {objective}",
                is_error=False,
            )

        output_lines = [
            f"Search: {objective}",
            f"Found {len(results)} result(s)",
            "-" * 60,
        ]

        for i, result in enumerate(results, 1):
            # Handle both dict and object access patterns
            if hasattr(result, 'url'):
                url = result.url or ""
                title = result.title or "No title"
                # SDK uses 'excerpts' (plural), may be list or string
                excerpts = getattr(result, 'excerpts', None) or ""
                if isinstance(excerpts, list):
                    excerpt = "\n".join(excerpts)
                else:
                    excerpt = str(excerpts) if excerpts else ""
            else:
                url = result.get("url", "")
                title = result.get("title", "No title")
                excerpts = result.get("excerpts", result.get("excerpt", ""))
                if isinstance(excerpts, list):
                    excerpt = "\n".join(excerpts)
                else:
                    excerpt = str(excerpts) if excerpts else ""

            output_lines.append(f"\n[{i}] {title}")
            output_lines.append(f"    URL: {url}")
            if excerpt:
                # Truncate very long excerpts
                if len(excerpt) > 500:
                    excerpt = excerpt[:500] + "..."
                output_lines.append(f"    {excerpt}")

        return ToolResult(
            call_id=call_id,
            content="\n".join(output_lines),
            is_error=False,
        )


class WebFetchTool:
    """
    Fetch and extract content from a URL using Parallel Extract API.

    Converts web pages to clean, LLM-optimized markdown.
    """

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key
        self._client: Parallel | None = None

    def _get_client(self):
        if self._client:
            return self._client

        api_key = self._api_key or _get_parallel_api_key()
        if not api_key:
            raise ValueError("PARALLEL_API_KEY not configured")

        self._client = Parallel(api_key=api_key)
        return self._client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web_fetch",
            summary="Fetch and extract content from a URL",
            tier=ToolTier.HOT,
            description="""Fetch and extract content from a URL.

Uses Parallel's Extract API to convert web pages into clean markdown
optimized for LLM consumption.

Features:
- Extracts main content, removes ads/navigation
- Converts to clean markdown
- Can focus on specific content with an objective
- Handles PDFs and complex pages

Use this after web_search to read the full content of relevant pages.""",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch and extract content from",
                    },
                    "objective": {
                        "type": "string",
                        "description": "Optional: what specific information you're looking for (helps focus extraction)",
                    },
                    "full_content": {
                        "type": "boolean",
                        "description": "Return full page content instead of focused excerpt (default: false)",
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute web fetch asynchronously with timeout."""
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    functools.partial(self._execute_sync, call_id, arguments),
                ),
                timeout=WEB_FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            url = arguments.get("url", "?")
            return ToolResult(
                call_id=call_id,
                content=f"Error: fetch timed out after {WEB_FETCH_TIMEOUT}s for {url}",
                is_error=True,
            )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        url = arguments.get("url", "")
        objective = arguments.get("objective", "")
        full_content = arguments.get("full_content", False)

        if not url:
            return ToolResult(
                call_id=call_id,
                content="Error: url is required",
                is_error=True,
            )

        # Basic URL validation
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            client = self._get_client()
        except ValueError as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: {e}",
                is_error=True,
            )

        try:
            # Use the SDK's beta.extract method - takes urls as a list
            extract_kwargs: dict[str, Any] = {"urls": [url]}
            if objective:
                extract_kwargs["objective"] = objective
            if full_content:
                extract_kwargs["full_content"] = True

            extract_response = client.beta.extract(**extract_kwargs)

            # Response has results list - get first result
            if not extract_response.results:
                return ToolResult(
                    call_id=call_id,
                    content=f"No content could be extracted from: {url}",
                    is_error=False,
                )

            result = extract_response.results[0]
            title = result.title or ""

            # SDK uses 'excerpts' (plural) and 'full_content'
            excerpts = result.excerpts or []
            full_text = result.full_content or ""

            if isinstance(excerpts, list):
                excerpt = "\n".join(excerpts)
            else:
                excerpt = str(excerpts) if excerpts else ""

            content = full_text if full_content else excerpt

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: Fetch request failed: {e}",
                is_error=True,
            )

        if not content:
            return ToolResult(
                call_id=call_id,
                content=f"No content could be extracted from: {url}",
                is_error=False,
            )

        output_lines = [
            f"URL: {url}",
        ]

        if title:
            output_lines.append(f"Title: {title}")

        output_lines.append("-" * 60)

        main_content = content

        if main_content:
            output_lines.append(main_content)
        else:
            output_lines.append("(No content extracted)")

        return ToolResult(
            call_id=call_id,
            content="\n".join(output_lines),
            is_error=False,
        )


class WebTaskTool:
    """
    Perform deep web research using Parallel Task API.

    For complex research questions that require synthesizing information
    from multiple sources.
    """

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key
        self._client: Parallel | None = None

    def _get_client(self):
        if self._client:
            return self._client

        api_key = self._api_key or _get_parallel_api_key()
        if not api_key:
            raise ValueError("PARALLEL_API_KEY not configured")

        self._client = Parallel(api_key=api_key)
        return self._client

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web_research",
            summary="Deep multi-source web research",
            tier=ToolTier.WARM,
            description="""Perform deep web research on a topic.

Uses Parallel's Task API to synthesize information from multiple web sources
and produce a comprehensive research result.

Best for:
- Complex questions requiring multiple sources
- Fact-finding and verification
- Comparative analysis
- Current events and recent information

Returns structured research with citations.""",
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The research question to investigate",
                    },
                    "depth": {
                        "type": "string",
                        "enum": ["lite", "base", "core", "pro"],
                        "description": "Research depth: lite (fast), base (balanced), core (thorough), pro (comprehensive). Default: base",
                    },
                },
                "required": ["question"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute deep research asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._execute_sync, call_id, arguments),
        )

    def _execute_sync(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        """Sync implementation for executor."""
        question = arguments.get("question", "")
        depth = arguments.get("depth", "base")

        if not question:
            return ToolResult(
                call_id=call_id,
                content="Error: question is required",
                is_error=True,
            )

        try:
            client = self._get_client()
        except ValueError as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: {e}",
                is_error=True,
            )

        # Map depth to processor
        processor_map = {
            "lite": "lite",
            "base": "base",
            "core": "core",
            "pro": "pro",
        }
        processor = processor_map.get(depth, "base")

        try:
            # Create and run task using SDK
            task_run = client.task_run.create(
                input=question,
                processor=processor,
            )

            # Get result (SDK handles polling)
            task_result = client.task_run.result(run_id=task_run.run_id)

        except Exception as e:
            return ToolResult(
                call_id=call_id,
                content=f"Error: Research request failed: {e}",
                is_error=True,
            )

        # Format result
        output_lines = [
            f"Research Question: {question}",
            f"Depth: {depth}",
            "-" * 60,
        ]

        # Handle both object and dict access patterns
        if hasattr(task_result, 'output'):
            output = task_result.output or ""
            sources = getattr(task_result, 'sources', []) or []
        else:
            output = task_result.get("output", "")
            sources = task_result.get("sources", [])

        if output:
            output_lines.append("\nAnswer:")
            output_lines.append(str(output))

        # Add sources if available
        if sources:
            output_lines.append("\nSources:")
            for source in sources[:5]:  # Limit to 5 sources
                if hasattr(source, 'url'):
                    url = source.url or ""
                    title = getattr(source, 'title', url) or url
                else:
                    url = source.get("url", "")
                    title = source.get("title", url)
                output_lines.append(f"  - {title}: {url}")

        return ToolResult(
            call_id=call_id,
            content="\n".join(output_lines),
            is_error=False,
        )
