"""Freyja bridge tool package.

Standalone copies of the non-ema tools. The only remaining
coupling with the engine package is `bridge.tools.base`, which
re-exports the Tool protocol types. When the app becomes its own
repository, replace that one shim file with vendored definitions.
"""

from bridge.tools.bash_tool import BashTool
from bridge.tools.browser_tools import BrowserExecuteJsTool, BrowserScreenshotTool
from bridge.tools.file_tools import (
    EditFileTool,
    ListDirectoryTool,
    ReadFileTool,
    WriteFileTool,
)
from bridge.tools.image_generation_tool import GenerateImageTool
from bridge.tools.kanban_board import KanbanTool, SessionKanbanBoard
from bridge.tools.video_analysis_tool import AnalyzeVideoTool
from bridge.tools.memory_tools import RecordUserPreferenceTool
from bridge.tools.registry import build_desktop_registry
from bridge.tools.search_tools import GlobTool, GrepTool
from bridge.tools.skill_tools import ListSkillsTool, LoadSkillTool, SearchSkillsTool

__all__ = [
    "AnalyzeVideoTool",
    "BashTool",
    "BrowserExecuteJsTool",
    "BrowserScreenshotTool",
    "EditFileTool",
    "GlobTool",
    "GrepTool",
    "GenerateImageTool",
    "KanbanTool",
    "ListDirectoryTool",
    "ListSkillsTool",
    "LoadSkillTool",
    "ReadFileTool",
    "RecordUserPreferenceTool",
    "SearchSkillsTool",
    "SessionKanbanBoard",
    "WriteFileTool",
    "build_desktop_registry",
]
