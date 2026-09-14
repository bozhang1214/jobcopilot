"""JobCopilot MCP Server（stdio / streamable-http）。"""
from typing import Any

from jobcopilot.mcp.config import ServerConfig
from jobcopilot.mcp.tools import ToolContext, ToolError

__all__ = ["ServerConfig", "ToolContext", "ToolError", "build_server", "main"]


def __getattr__(name: str) -> Any:
    """延迟导入 server，避免没装 mcp 依赖时 import 本包就失败。"""
    if name in {"build_server", "main"}:
        from jobcopilot.mcp import server

        return getattr(server, name)
    raise AttributeError(name)
