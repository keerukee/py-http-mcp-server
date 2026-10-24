"""Per-request context, available to handlers via :func:`get_current_request`."""

import contextvars
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .transport import HttpRequest

__all__ = ["get_current_request", "current_base_url"]

_current_request: "contextvars.ContextVar[Optional[HttpRequest]]" = contextvars.ContextVar(
    "py_http_mcp_server_current_request", default=None
)


def get_current_request() -> "Optional[HttpRequest]":
    """Return the HTTP request currently being processed (headers, authenticated ``user``, ...), if any."""
    return _current_request.get()


def current_base_url(default: str = "http://localhost") -> str:
    req = _current_request.get()
    return req.base_url if req is not None else default
