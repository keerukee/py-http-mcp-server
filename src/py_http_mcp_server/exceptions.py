"""Exception types used by the MCP HTTP server."""

from typing import Any, Optional

__all__ = ["McpError", "McpException", "McpCancelledError", "McpConfigurationError"]


class McpError(Exception):
    """A protocol-level MCP / JSON-RPC error carrying a JSON-RPC error ``code``."""

    # Standard JSON-RPC error codes
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error_object(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err

    @classmethod
    def parse_error(cls, message: str) -> "McpError":
        return cls(cls.PARSE_ERROR, message)

    @classmethod
    def invalid_request(cls, message: str) -> "McpError":
        return cls(cls.INVALID_REQUEST, message)

    @classmethod
    def method_not_found(cls, method: Optional[str]) -> "McpError":
        return cls(cls.METHOD_NOT_FOUND, f"Method not found: {method or 'unknown'}")

    @classmethod
    def invalid_params(cls, message: str) -> "McpError":
        return cls(cls.INVALID_PARAMS, message)

    @classmethod
    def internal_error(cls, message: str) -> "McpError":
        return cls(cls.INTERNAL_ERROR, message)


#: Alias matching the .NET library naming.
McpException = McpError


class McpCancelledError(Exception):
    """Raised when an operation observes that its :class:`CancellationToken` was cancelled."""

    def __init__(self, message: str = "Operation was cancelled.") -> None:
        super().__init__(message)


class McpConfigurationError(ValueError):
    """Raised for invalid server configuration or invalid handler registration."""
