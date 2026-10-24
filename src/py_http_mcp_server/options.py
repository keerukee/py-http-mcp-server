"""Server configuration (mirrors ``McpServerOptions`` from the .NET library)."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Sequence, Set

from .content import McpIcon

if TYPE_CHECKING:  # pragma: no cover
    from .transport import HttpRequest

__all__ = [
    "McpServerOptions",
    "OAuthProtectedResourceOptions",
    "DEFAULT_SUPPORTED_PROTOCOL_VERSIONS",
    "Authenticator",
]

#: Protocol revisions supported out of the box.
DEFAULT_SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

#: ``authenticator(request) -> principal``. Return a truthy value (e.g. user info) to allow the request,
#: or a falsy value to reject it with ``401 Unauthorized``.
Authenticator = Callable[["HttpRequest"], Any]


@dataclass
class OAuthProtectedResourceOptions:
    """RFC 9728 OAuth 2.0 Protected Resource Metadata (``/.well-known/oauth-protected-resource``)."""

    #: Resource identifier URL (defaults to ``<base-url><endpoint>``).
    resource: Optional[str] = None
    #: OAuth 2.0 / OIDC authorization server issuer URLs.
    authorization_servers: List[str] = field(default_factory=list)
    #: OAuth scopes supported by this MCP resource server.
    scopes_supported: List[str] = field(default_factory=list)
    #: Supported bearer token presentation methods.
    bearer_methods_supported: List[str] = field(default_factory=lambda: ["header"])
    #: Whether OAuth Client ID Metadata Documents are supported (MCP SEP-991).
    client_id_metadata_document_supported: bool = True


@dataclass
class McpServerOptions:
    """Configuration for the MCP Streamable HTTP server."""

    #: Server name shown to clients.
    server_name: str = "MCP-HTTP-Server"
    #: Server version shown to clients.
    server_version: str = "1.0.0"
    #: Optional human-readable title (MCP SEP-973).
    server_title: Optional[str] = None
    #: Optional website URL (MCP SEP-973).
    website_url: Optional[str] = None
    #: Optional server icons; relative paths are resolved against the request host.
    icons: List[McpIcon] = field(default_factory=list)
    #: Default protocol version advertised when the client doesn't request a supported one.
    protocol_version: str = "2025-11-25"
    #: Protocol versions accepted during negotiation and header validation.
    supported_protocol_versions: Sequence[str] = DEFAULT_SUPPORTED_PROTOCOL_VERSIONS
    #: Optional usage instructions returned by ``initialize`` / ``server/discover``.
    instructions: Optional[str] = None
    #: Log MCP request processing.
    enable_logging: bool = True
    #: Log HTTP requests hitting the MCP endpoint.
    enable_http_logging: bool = True
    #: Route path of the MCP endpoint.
    endpoint: str = "/mcp"
    #: Protect the MCP endpoint with ``authenticator``.
    require_authorization: bool = False
    #: Callback used when ``require_authorization`` is True (replaces ASP.NET Core auth policies).
    authenticator: Optional[Authenticator] = None
    #: Add permissive CORS headers (``Access-Control-Allow-Origin: *``) and answer preflight requests.
    use_default_cors: bool = False
    #: Pure stateless mode: never emit ``Mcp-Session-Id`` headers.
    stateless_mode: bool = False
    #: Reject unsupported ``MCP-Protocol-Version`` headers with HTTP 400.
    validate_protocol_version_header: bool = True
    #: Reject ``Mcp-Method`` / ``Mcp-Name`` headers that don't match the JSON-RPC body (SEP-2243).
    validate_routing_headers: bool = True
    #: Optional ``ttlMs`` caching hint for list responses.
    list_cache_ttl_ms: Optional[int] = None
    #: ``cacheScope`` advertised on cacheable responses (``"public"`` or ``"private"``).
    cache_scope: str = "public"
    #: Enable the ``io.modelcontextprotocol/tasks`` extension (SEP-2663).
    enable_tasks_extension: bool = True
    #: When non-empty, requests whose ``Origin`` is not listed are rejected with HTTP 403.
    allowed_origins: Set[str] = field(default_factory=set)
    #: Optional RFC 9728 metadata for OAuth discovery.
    oauth_protected_resource: Optional[OAuthProtectedResourceOptions] = None

    # ---- Python-specific knobs -------------------------------------------------------------
    #: Maximum accepted request body size in bytes (HTTP 413 above this).
    max_request_body_bytes: int = 4 * 1024 * 1024
    #: Seconds between SSE keep-alive comments on GET streams (also detects disconnects).
    sse_keepalive_interval: float = 15.0
    #: Send an initial ``endpoint`` event on GET streams (as the .NET server does). Pure Streamable HTTP
    #: clients ignore it (some log "Unknown SSE event"); set False to omit it.
    sse_endpoint_event: bool = True
    #: Worker threads used for asynchronous (tasks extension) tool execution.
    task_max_workers: int = 8

    def __post_init__(self) -> None:
        if not self.endpoint.startswith("/"):
            self.endpoint = "/" + self.endpoint
        if len(self.endpoint) > 1:
            self.endpoint = self.endpoint.rstrip("/")
        self.supported_protocol_versions = tuple(self.supported_protocol_versions)
        self.allowed_origins = set(self.allowed_origins)

    # Helpers -----------------------------------------------------------------------------------
    def is_supported_version(self, version: Optional[str]) -> bool:
        if not version:
            return False
        lowered = version.lower()
        return any(v.lower() == lowered for v in self.supported_protocol_versions)

    def is_origin_allowed(self, origin: str) -> bool:
        lowered = origin.lower()
        return any(o.lower() == lowered for o in self.allowed_origins)
