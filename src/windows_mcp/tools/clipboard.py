"""Clipboard tool — copy/paste clipboard operations."""

from typing import Literal

from mcp.types import ToolAnnotations
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context


def execute_clipboard(mode: Literal["get", "set"], text: str | None = None) -> str:
    import win32clipboard

    if mode == "get":
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return f"Clipboard content:\n{data}"
            return "Clipboard is empty or contains non-text data."
        finally:
            win32clipboard.CloseClipboard()
    if mode == "set":
        if text is None:
            return "Error: text parameter required for set mode."
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            return f"Clipboard set to: {text[:100]}{'...' if len(text) > 100 else ''}"
        finally:
            win32clipboard.CloseClipboard()
    return 'Error: mode must be either "get" or "set".'


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Clipboard",
        description='Copy/paste clipboard operations. Keywords: copy, paste, cut, clipboard, text transfer. Use mode="get" to read current clipboard content, mode="set" to set clipboard text. This is the real Windows clipboard; set overwrites shared contents. Avoid using it merely to pass data between tools and coordinate known interference with the user. Inside multi-step flows (e.g. set clipboard then Ctrl+V), prefer Sequence\'s inline clipboard step.',
        annotations=ToolAnnotations(
            title="Clipboard",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Clipboard-Tool")
    def clipboard_tool(
        mode: Literal["get", "set"],
        text: str | None = None,
        ctx: Context = None,
    ) -> str:
        return execute_clipboard(mode=mode, text=text)
