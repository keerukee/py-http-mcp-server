"""py-http-mcp-server: a zero-dependency Model Context Protocol (MCP) server over Streamable HTTP.

Supports MCP protocol revisions 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 and 2026-07-28.
"""

from ._version import __version__
from .asgi import McpAsgiApp, McpAsgiMiddleware
from .auth import bearer_token_authenticator
from .cancellation import CancellationToken
from .content import McpContent, McpIcon, McpToolResult
from .context import get_current_request
from .decorators import (
    McpArgument,
    McpParameter,
    Param,
    mcp_handler,
    mcp_icon,
    mcp_prompt,
    mcp_resource,
    mcp_resource_template,
    mcp_tool,
)
from .exceptions import McpCancelledError, McpConfigurationError, McpError, McpException
from .options import McpServerOptions, OAuthProtectedResourceOptions
from .processor import McpRequestProcessor
from .registry import McpRegistry
from .server import McpServer
from .sse import SessionManager, SseSession
from .tasks import McpTask, McpTaskStore
from .transport import Headers, HttpError, HttpRequest, HttpResponse, McpHttpApp, sse_response
from .wsgi import McpWsgiMiddleware

__all__ = [
    "__version__",
    # server
    "McpServer",
    "McpServerOptions",
    "OAuthProtectedResourceOptions",
    # framework integration
    "McpAsgiApp",
    "McpAsgiMiddleware",
    "McpWsgiMiddleware",
    # decorators
    "mcp_handler",
    "mcp_tool",
    "mcp_resource",
    "mcp_resource_template",
    "mcp_prompt",
    "mcp_icon",
    "Param",
    "McpParameter",
    "McpArgument",
    # content
    "McpContent",
    "McpToolResult",
    "McpIcon",
    # runtime helpers
    "CancellationToken",
    "get_current_request",
    "bearer_token_authenticator",
    # errors
    "McpError",
    "McpException",
    "McpCancelledError",
    "McpConfigurationError",
    # building blocks
    "McpRegistry",
    "McpRequestProcessor",
    "McpTask",
    "McpTaskStore",
    "McpHttpApp",
    "HttpRequest",
    "HttpResponse",
    "HttpError",
    "sse_response",
    "Headers",
    "SseSession",
    "SessionManager",
]
