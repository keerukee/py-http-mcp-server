"""Framework-agnostic Streamable HTTP transport.

:class:`McpHttpApp` maps an :class:`HttpRequest` to an :class:`HttpResponse`. Thin adapters
(stdlib ``http.server``, WSGI) translate to/from the wire.

Besides the MCP endpoint, the app can host a regular web application: custom routes
(:meth:`McpHttpApp.add_route`) and static file directories (:meth:`McpHttpApp.add_static`).
"""

import logging
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Pattern, Sequence, Tuple, Union
from urllib.parse import parse_qs

from . import _json
from .context import _current_request
from .exceptions import McpConfigurationError, McpError
from .options import McpServerOptions
from .processor import McpRequestProcessor, has_initialize_handshake, negotiate_initialize_version
from .registry import McpRegistry
from .sse import SessionManager, SseSession

__all__ = [
    "Headers",
    "HttpRequest",
    "HttpResponse",
    "HttpError",
    "McpHttpApp",
    "Route",
    "sse_response",
    "WELL_KNOWN_OAUTH_PATH",
]

logger = logging.getLogger("py_http_mcp_server")

WELL_KNOWN_OAUTH_PATH = "/.well-known/oauth-protected-resource"
_EXPOSED_HEADERS = "Mcp-Session-Id, MCP-Protocol-Version, Mcp-Method, Mcp-Name"
_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
_ROUTE_VAR = re.compile(r"\{(\w+)\}")

# Explicit content types (the Windows registry sometimes maps .js to text/plain).
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json",
    ".woff2": "font/woff2",
}


class HttpError(Exception):
    """Raise from a web route handler to return an HTTP error as ``{"error": message}`` JSON."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Headers:
    """Minimal case-insensitive, multi-value header collection."""

    def __init__(self, items: Union[None, Mapping[str, str], Iterable[Tuple[str, str]]] = None) -> None:
        self._items: List[Tuple[str, str]] = []
        if items is None:
            return
        pairs = items.items() if isinstance(items, Mapping) else items
        for key, value in pairs:
            self._items.append((str(key), str(value)))

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        lowered = name.lower()
        for key, value in self._items:
            if key.lower() == lowered:
                return value
        return default

    def get_all(self, name: str) -> List[str]:
        lowered = name.lower()
        return [v for k, v in self._items if k.lower() == lowered]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self) -> Iterator[Tuple[str, str]]:
        return iter(self._items)

    def __repr__(self) -> str:
        return f"Headers({self._items!r})"


@dataclass
class HttpRequest:
    """Transport-neutral HTTP request."""

    method: str
    path: str
    headers: Headers = field(default_factory=Headers)
    body: bytes = b""
    query_string: str = ""
    scheme: str = "http"
    remote_addr: Optional[str] = None
    #: Principal returned by the configured ``authenticator`` (set after successful auth).
    user: Any = None
    #: Values captured from ``{name}`` placeholders of a matched web route.
    path_params: Dict[str, str] = field(default_factory=dict)
    #: Mount prefix of the hosting application (ASGI ``root_path`` / WSGI ``SCRIPT_NAME``).
    root_path: str = ""

    def header(self, name: str) -> Optional[str]:
        value = self.headers.get(name)
        return value if value else None

    @property
    def query(self) -> Dict[str, str]:
        """Query-string parameters (first value per key)."""
        return {k: v[0] for k, v in parse_qs(self.query_string, keep_blank_values=True).items()}

    @property
    def text(self) -> str:
        return self.body.decode("utf-8-sig") if self.body else ""

    def json(self) -> Any:
        """Decode the JSON body (``None`` if empty). Raises :class:`HttpError` 400 on invalid JSON."""
        if not self.body:
            return None
        try:
            return _json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError(400, f"Invalid JSON body: {exc}") from None

    @property
    def host(self) -> str:
        return self.headers.get("Host") or "localhost"

    @property
    def base_url(self) -> str:
        """``scheme://host`` plus the application's mount prefix (``root_path``)."""
        return f"{self.scheme}://{self.host}{self.root_path.rstrip('/')}"

    @property
    def session_id(self) -> Optional[str]:
        return self.header("Mcp-Session-Id")


@dataclass
class HttpResponse:
    """Transport-neutral HTTP response. ``stream`` (if set) is an iterator of body chunks (SSE)."""

    status: int = 200
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    stream: Optional[Iterator[bytes]] = None
    #: Called by transports when the client disconnects from a ``stream`` (ends the stream promptly).
    on_close: Optional[Callable[[], None]] = None

    def set_header(self, name: str, value: str) -> None:
        lowered = name.lower()
        self.headers = [(k, v) for k, v in self.headers if k.lower() != lowered]
        self.headers.append((name, value))

    def get_header(self, name: str) -> Optional[str]:
        lowered = name.lower()
        return next((v for k, v in self.headers if k.lower() == lowered), None)

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "HttpResponse":
        return cls(status=status, headers=[("Content-Type", "application/json")], body=_json.dumps(payload).encode("utf-8"))

    @classmethod
    def text(cls, text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> "HttpResponse":
        return cls(status=status, headers=[("Content-Type", content_type)], body=text.encode("utf-8"))

    @classmethod
    def html(cls, html: str, status: int = 200) -> "HttpResponse":
        return cls.text(html, "text/html; charset=utf-8", status)

    @classmethod
    def redirect(cls, location: str, status: int = 302) -> "HttpResponse":
        return cls(status=status, headers=[("Location", location)])


def sse_response(manager: SessionManager, keepalive_interval: float = 15.0,
                 session_id: Optional[str] = None, initial_event: Optional[Tuple[str, str]] = None) -> HttpResponse:
    """Open a browser-friendly Server-Sent Events stream registered in ``manager``.

    Use ``manager.broadcast(obj)`` to push JSON ``message`` events to all subscribers
    (``EventSource.onmessage`` in the browser).
    """
    session = SseSession(session_id or str(uuid.uuid4()), keepalive_interval=keepalive_interval)
    manager.add(session)
    if initial_event is not None:
        session.send_event(*initial_event)

    def _stream() -> Iterator[bytes]:
        try:
            yield b"retry: 3000\n\n"
            yield from session.iter_stream()
        finally:
            manager.remove(session.session_id, session)
            session.close()

    return HttpResponse(
        status=200,
        headers=[("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache"), ("X-Accel-Buffering", "no")],
        stream=_stream(),
        on_close=session.close,
    )


RouteHandler = Callable[[HttpRequest], Any]


@dataclass
class Route:
    """A web route: ``path`` may contain ``{name}`` placeholders (one path segment each)."""

    path: str
    methods: frozenset
    handler: RouteHandler
    require_auth: bool = False
    pattern: Optional[Pattern[str]] = None

    def __post_init__(self) -> None:
        parts: List[str] = []
        last = 0
        for match in _ROUTE_VAR.finditer(self.path):
            parts.append(re.escape(self.path[last:match.start()]))
            parts.append(f"(?P<{match.group(1)}>[^/]+)")
            last = match.end()
        parts.append(re.escape(self.path[last:]))
        self.pattern = re.compile("^" + "".join(parts) + "$")


def _coerce_response(result: Any) -> HttpResponse:
    """Turn a route handler's return value into an :class:`HttpResponse`."""
    if isinstance(result, HttpResponse):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        response = _coerce_response(result[0])
        response.status = result[1]
        return response
    if result is None:
        return HttpResponse(status=204)
    if isinstance(result, str):
        return HttpResponse.html(result)
    if isinstance(result, (bytes, bytearray)):
        return HttpResponse(headers=[("Content-Type", "application/octet-stream")], body=bytes(result))
    return HttpResponse.json(result)


def _jsonrpc_error(code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


class McpHttpApp:
    """The MCP Streamable HTTP endpoint logic (POST / GET / DELETE / OAuth discovery / CORS / auth),
    plus optional web routes and static files so one server can be both a web app and an MCP server."""

    def __init__(self, options: McpServerOptions, registry: McpRegistry, processor: McpRequestProcessor,
                 sessions: Optional[SessionManager] = None) -> None:
        self.options = options
        self.registry = registry
        self.processor = processor
        self.sessions = sessions if sessions is not None else SessionManager()
        self.routes: List[Route] = []
        self.static_mounts: List[Tuple[str, str, str]] = []

    # ---- web app registration ----------------------------------------------------------------
    def add_route(self, path: str, handler: RouteHandler, methods: Sequence[str] = ("GET",),
                  require_auth: bool = False) -> Route:
        """Register a web route. ``handler(request)`` may return an :class:`HttpResponse`, ``str`` (HTML),
        ``dict``/``list`` (JSON), ``bytes``, ``None`` (204) or ``(body, status)``.

        ``require_auth=True`` applies the server's ``authenticator`` to this route.
        """
        normalized = "/" + path.strip("/") if path.strip("/") else "/"
        if normalized == self.options.endpoint:
            raise McpConfigurationError(f"Route '{path}' conflicts with the MCP endpoint")
        if require_auth and self.options.authenticator is None:
            raise McpConfigurationError("require_auth=True needs McpServerOptions.authenticator")
        route = Route(normalized, frozenset(m.upper() for m in methods), handler, require_auth)
        self.routes.append(route)
        return route

    def add_static(self, prefix: str, directory: str, index: str = "index.html") -> None:
        """Serve files from ``directory`` under URL ``prefix`` (``GET``/``HEAD``; path traversal is blocked)."""
        if not os.path.isdir(directory):
            raise McpConfigurationError(f"Static directory not found: {directory}")
        normalized = "/" + prefix.strip("/") if prefix.strip("/") else "/"
        self.static_mounts.append((normalized, os.path.realpath(directory), index))
        self.static_mounts.sort(key=lambda m: len(m[0]), reverse=True)  # longest prefix wins

    @property
    def serves_oauth_metadata(self) -> bool:
        return self.options.require_authorization or self.options.oauth_protected_resource is not None

    def is_mcp_path(self, path: str, include_web_routes: bool = True) -> bool:
        """True if this app should handle ``path`` when embedded as middleware in another web framework.

        Covers the MCP endpoint, the OAuth well-known path (when enabled) and, optionally, routes
        registered with :meth:`add_route` (static mounts are never claimed, so they can't shadow the host app).
        """
        path = path.rstrip("/") or "/"
        if path == self.options.endpoint:
            return True
        if self.serves_oauth_metadata and (path == WELL_KNOWN_OAUTH_PATH or path.startswith(WELL_KNOWN_OAUTH_PATH + "/")):
            return True
        if include_web_routes:
            return any(route.pattern is not None and route.pattern.match(path) for route in self.routes)
        return False

    # ---- entry point -------------------------------------------------------------------------
    def handle(self, request: HttpRequest) -> HttpResponse:
        token = _current_request.set(request)
        try:
            response = self._route(request)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            logger.exception("Unhandled error processing %s %s", request.method, request.path)
            response = HttpResponse.json(_jsonrpc_error(McpError.INTERNAL_ERROR, str(exc) or "Internal error"), 500)
        finally:
            _current_request.reset(token)
        if self.options.use_default_cors:
            response.set_header("Access-Control-Allow-Origin", "*")
            response.set_header("Access-Control-Expose-Headers", _EXPOSED_HEADERS)
        return response

    def _route(self, request: HttpRequest) -> HttpResponse:
        opts = self.options
        path = request.path.rstrip("/") or "/"
        method = request.method.upper()
        is_endpoint = path == opts.endpoint

        if is_endpoint and opts.enable_http_logging:
            logger.info("[MCP HTTP] %s %s", method, path)

        if method == "OPTIONS" and opts.use_default_cors:
            return self._preflight(request)

        if path == WELL_KNOWN_OAUTH_PATH or path.startswith(WELL_KNOWN_OAUTH_PATH + "/"):
            if (opts.require_authorization or opts.oauth_protected_resource is not None) and method in ("GET", "HEAD"):
                return self._oauth_metadata(request)
            return HttpResponse(status=404)

        if not is_endpoint:
            return self._handle_web(request, path, method)

        if opts.require_authorization:
            denied = self._authenticate(request)
            if denied is not None:
                return denied

        if method == "POST":
            return self._handle_post(request)
        if method == "GET":
            return self._handle_get(request)
        if method == "DELETE":
            return self._handle_delete(request)
        return HttpResponse(status=405, headers=[("Allow", "GET, POST, DELETE")])

    # ---- web app -----------------------------------------------------------------------------
    def _handle_web(self, request: HttpRequest, path: str, method: str) -> HttpResponse:
        effective = "GET" if method == "HEAD" else method
        allowed: set = set()
        for route in self.routes:
            match = route.pattern.match(path) if route.pattern else None
            if not match:
                continue
            if effective not in route.methods:
                allowed |= route.methods
                continue
            if route.require_auth:
                denied = self._authenticate(request)
                if denied is not None:
                    return denied
            request.path_params = match.groupdict()
            try:
                return _coerce_response(route.handler(request))
            except HttpError as exc:
                return HttpResponse.json({"error": exc.message or "Error"}, exc.status)

        if effective == "GET":
            for prefix, directory, index in self.static_mounts:
                if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
                    response = self._serve_static(path[len(prefix):] if prefix != "/" else path, directory, index)
                    if response is not None:
                        return response

        if allowed:
            return HttpResponse(status=405, headers=[("Allow", ", ".join(sorted(allowed)))])
        return HttpResponse.json({"error": "Not found"}, 404)

    @staticmethod
    def _serve_static(relative: str, directory: str, index: str) -> Optional[HttpResponse]:
        segments = [s for s in relative.replace("\\", "/").split("/") if s not in ("", ".")]
        if any(s == ".." or ":" in s for s in segments):
            return HttpResponse(status=404)
        target = os.path.realpath(os.path.join(directory, *segments))
        try:
            if os.path.commonpath([directory, target]) != directory:
                return HttpResponse(status=404)
        except ValueError:  # different drives on Windows
            return HttpResponse(status=404)
        if os.path.isdir(target):
            target = os.path.join(target, index)
        if not os.path.isfile(target):
            return None
        ext = os.path.splitext(target)[1].lower()
        content_type = _CONTENT_TYPES.get(ext) or mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            body = fh.read()
        return HttpResponse(headers=[("Content-Type", content_type), ("Cache-Control", "no-cache")], body=body)

    # ---- cross-cutting -----------------------------------------------------------------------
    def _preflight(self, request: HttpRequest) -> HttpResponse:
        requested_headers = request.header("Access-Control-Request-Headers") or "*"
        return HttpResponse(
            status=204,
            headers=[
                ("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS"),
                ("Access-Control-Allow-Headers", requested_headers),
                ("Access-Control-Max-Age", "86400"),
            ],
        )

    def _authenticate(self, request: HttpRequest) -> Optional[HttpResponse]:
        authenticator = self.options.authenticator
        try:
            principal = authenticator(request) if authenticator is not None else None
        except Exception:  # noqa: BLE001 - treat authenticator failures as unauthenticated
            logger.exception("Authenticator raised an exception")
            principal = None
        if isinstance(principal, HttpResponse):
            return principal
        if principal:
            request.user = principal
            return None
        return self.unauthorized_response(request)

    def unauthorized_response(self, request: HttpRequest) -> HttpResponse:
        """401 with RFC 9728 ``WWW-Authenticate`` discovery header and a JSON OAuth error body."""
        www_auth = f'Bearer resource_metadata="{request.base_url}{WELL_KNOWN_OAUTH_PATH}"'
        oauth = self.options.oauth_protected_resource
        if oauth is not None and oauth.scopes_supported:
            www_auth += f', scope="{" ".join(oauth.scopes_supported)}"'
        response = HttpResponse.json(
            {"error": "unauthorized", "error_description": "Authentication is required to access this MCP server."}, 401
        )
        response.set_header("WWW-Authenticate", www_auth)
        return response

    def _oauth_metadata(self, request: HttpRequest) -> HttpResponse:
        oauth = self.options.oauth_protected_resource
        return HttpResponse.json(
            {
                "resource": (oauth.resource if oauth and oauth.resource else f"{request.base_url}{self.options.endpoint}"),
                "authorization_servers": list(oauth.authorization_servers) if oauth else [],
                "scopes_supported": list(oauth.scopes_supported) if oauth else [],
                "bearer_methods_supported": list(oauth.bearer_methods_supported) if oauth else ["header"],
                "client_id_metadata_document_supported": oauth.client_id_metadata_document_supported if oauth else True,
            }
        )

    def _validate_origin(self, request: HttpRequest) -> Optional[HttpResponse]:
        if not self.options.allowed_origins:
            return None
        origin = request.header("Origin")
        if origin and not self.options.is_origin_allowed(origin):
            return HttpResponse.json(_jsonrpc_error(McpError.INVALID_REQUEST, f"Origin '{origin}' is not allowed."), 403)
        return None

    def _validate_protocol_version(self, request: HttpRequest) -> Tuple[Optional[HttpResponse], str]:
        negotiated = self.options.protocol_version
        requested = request.header("MCP-Protocol-Version")
        if requested:
            if self.options.validate_protocol_version_header and not self.options.is_supported_version(requested):
                supported = ", ".join(self.options.supported_protocol_versions)
                return HttpResponse.json(
                    _jsonrpc_error(
                        McpError.INVALID_REQUEST,
                        f"Unsupported MCP-Protocol-Version '{requested}'. Supported versions: {supported}",
                    ),
                    400,
                ), negotiated
            negotiated = requested
        return None, negotiated

    def _body_protocol_version(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
            return None
        params = payload["params"]
        is_initialize = payload.get("method") == "initialize"
        meta = params.get("_meta")
        if isinstance(meta, dict):
            version = meta.get(_META_PROTOCOL_VERSION)
            if isinstance(version, str) and version and self.options.is_supported_version(version):
                return negotiate_initialize_version(version, self.options) if is_initialize else version
        version = params.get("protocolVersion")
        if isinstance(version, str) and version:
            return negotiate_initialize_version(version, self.options)
        return None

    # ---- POST: JSON-RPC ----------------------------------------------------------------------
    def _handle_post(self, request: HttpRequest) -> HttpResponse:
        opts = self.options
        error = self._validate_origin(request)
        if error is not None:
            return error
        error, negotiated = self._validate_protocol_version(request)
        if error is not None:
            return error

        session_id = request.session_id
        try:
            payload = _json.loads(request.body)
        except (ValueError, UnicodeDecodeError) as exc:
            if opts.enable_logging:
                logger.info("[MCP Parse Error] %s", exc)
            return HttpResponse.json(self.processor.parse_error_response(exc), 400)

        if opts.validate_routing_headers:
            header_error = self.processor.validate_routing_headers(
                payload, request.header("Mcp-Method"), request.header("Mcp-Name")
            )
            if header_error:
                return HttpResponse.json(_jsonrpc_error(McpError.INVALID_REQUEST, header_error), 400)

        messages = payload if isinstance(payload, list) else [payload]
        is_initialize = any(isinstance(m, dict) and m.get("method") == "initialize" for m in messages)
        if not opts.stateless_mode and not session_id and is_initialize:
            session_id = str(uuid.uuid4())

        response_obj = self.processor.process_payload(payload)

        body_version = self._body_protocol_version(payload) if request.header("MCP-Protocol-Version") is None else None
        if body_version:
            negotiated = body_version
        elif is_initialize and not has_initialize_handshake(negotiated):
            negotiated = negotiate_initialize_version(negotiated, opts)

        headers = [("MCP-Protocol-Version", negotiated)]
        if not opts.stateless_mode and session_id:
            headers.append(("Mcp-Session-Id", session_id))

        if response_obj is None:
            return HttpResponse(status=202, headers=headers)

        response_json = _json.dumps(response_obj)
        accept = (request.header("Accept") or "").lower()
        if accept and "text/event-stream" in accept and "application/json" not in accept and "*/*" not in accept:
            headers += [("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache")]
            return HttpResponse(status=200, headers=headers,
                                body=f"id: 1\nevent: message\ndata: {response_json}\n\n".encode("utf-8"))

        headers.append(("Content-Type", "application/json"))
        return HttpResponse(status=200, headers=headers, body=response_json.encode("utf-8"))

    # ---- GET: SSE stream ---------------------------------------------------------------------
    def _handle_get(self, request: HttpRequest) -> HttpResponse:
        opts = self.options
        error = self._validate_origin(request)
        if error is not None:
            return error
        error, negotiated = self._validate_protocol_version(request)
        if error is not None:
            return error

        session_id = request.session_id or str(uuid.uuid4())
        session = SseSession(session_id, request.header("Last-Event-ID"), opts.sse_keepalive_interval)
        self.sessions.add(session)
        if opts.enable_logging:
            logger.info("[MCP] Stream Connected: %s", session_id)
        # Initial event (parity with the .NET server; compatible with legacy HTTP+SSE clients).
        if opts.sse_endpoint_event:
            session.send_event("endpoint", f"{request.base_url}{opts.endpoint}")

        headers = [
            ("Content-Type", "text/event-stream"),
            ("Cache-Control", "no-cache"),
            ("X-Accel-Buffering", "no"),
            ("MCP-Protocol-Version", negotiated),
        ]
        if not opts.stateless_mode:
            headers.append(("Mcp-Session-Id", session_id))

        sessions = self.sessions
        enable_logging = opts.enable_logging

        def _stream() -> Iterator[bytes]:
            try:
                yield from session.iter_stream()
            finally:
                sessions.remove(session_id, session)
                session.close()
                if enable_logging:
                    logger.info("[MCP] Stream Disconnected: %s", session_id)

        return HttpResponse(status=200, headers=headers, stream=_stream(), on_close=session.close)

    # ---- DELETE: terminate session -----------------------------------------------------------
    def _handle_delete(self, request: HttpRequest) -> HttpResponse:
        error = self._validate_origin(request)
        if error is not None:
            return error
        session_id = request.session_id
        if session_id and self.sessions.remove(session_id) is not None and self.options.enable_logging:
            logger.info("[MCP] Session Explicitly Terminated: %s", session_id)
        return HttpResponse(status=200)
