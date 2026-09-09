"""Claude-Code-style plugin support (design doc section 5)."""

from bridge.plugins.commands import (
    format_plugin_table,
    handle_plugin_command,
    parse_plugin_args,
)
from bridge.plugins.loader import (
    PluginError,
    install,
    list_installed,
    uninstall,
)

__all__ = [
    "PluginError",
    "format_plugin_table",
    "handle_plugin_command",
    "install",
    "list_installed",
    "parse_plugin_args",
    "uninstall",
]
