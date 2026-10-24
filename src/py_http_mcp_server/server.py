"""High-level :class:`McpServer` facade tying registry, processor and HTTP transport together."""

import dataclasses
import importlib
import inspect
import logging
import pkgutil
import ssl
import sys
import types
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Union

from ._async import AsyncRunner
from ._version import __version__
from .asgi import McpAsgiApp, McpAsgiMiddleware
from .exceptions import McpConfigurationError
from .http_server import McpThreadingHTTPServer
from .options import McpServerOptions
from .processor import McpRequestProcessor
from .registry import McpRegistry
from .sse import SessionManager
from .tasks import McpTaskStore
from .transport import WELL_KNOWN_OAUTH_PATH, HttpRequest, HttpResponse, McpHttpApp
from .wsgi import McpWsgiMiddleware, make_wsgi_app

__all__ = ["McpServer"]

logger = logging.getLogger("py_http_mcp_server")


class McpServer:
    """An MCP server exposed over Streamable HTTP.

    Standalone (stdlib HTTP server)::

        mcp = McpServer(server_name="MyServer", instructions="Use these tools ...")

        @mcp.tool()
        def calculate_sum(a: int, b: int) -> int:
            \"\"\"Calculates the sum of two numbers.\"\"\"
            return a + b

        mcp.run(port=8000)          # http://127.0.0.1:8000/mcp

    Inside an existing web app (FastAPI / Starlette / Flask)::

        mcp.mount(app)              # adds /mcp (+ OAuth discovery when configured)
    """

    def __init__(self, options: Optional[McpServerOptions] = None, **overrides: Any) -> None:
        options = options or McpServerOptions()
        if overrides:
            valid = {f.name for f in dataclasses.fields(McpServerOptions)}
            unknown = set(overrides) - valid
            if unknown:
                raise McpConfigurationError(f"Unknown McpServerOptions field(s): {', '.join(sorted(unknown))}")
            options = dataclasses.replace(options, **overrides)
        if options.require_authorization and options.authenticator is None:
            raise McpConfigurationError("require_authorization=True requires an 'authenticator' callback")

        self.options = options
        self.runner = AsyncRunner()
        self.registry = McpRegistry(self.runner)
        self.task_store = McpTaskStore()
        self.processor = McpRequestProcessor(self.registry, options, self.task_store)
        self.sessions = SessionManager()
        self.app = McpHttpApp(options, self.registry, self.processor, self.sessions)
        self._http_server: Optional[McpThreadingHTTPServer] = None
        self._asgi_app: Optional[McpAsgiApp] = None

        # Decorator shortcuts
        self.tool = self.registry.tool
        self.resource = self.registry.resource
        self.resource_template = self.registry.resource_template
        self.prompt = self.registry.prompt

    # ---- registration ------------------------------------------------------------------------
    def register(self, *targets: Any) -> "McpServer":
        """Register modules, ``@mcp_handler`` classes, instances or decorated functions."""
        self.registry.register(*targets)
        return self

    def discover(self, *targets: Union[str, types.ModuleType]) -> "McpServer":
        """Import and scan modules/packages for ``@mcp_tool`` functions and ``@mcp_handler`` classes.

        The Python counterpart of the .NET library's assembly scanning. ``targets`` are module objects
        or dotted names (``"myapp.tools"``); packages are scanned recursively. With no arguments the
        caller's module is scanned.
        """
        if not targets:
            caller = sys._getframe(1).f_globals.get("__name__", "__main__")
            targets = (sys.modules[caller],)
        for target in targets:
            module = importlib.import_module(target) if isinstance(target, str) else target
            self.registry.register_module(module)
            if hasattr(module, "__path__"):  # package -> walk submodules
                for info in pkgutil.walk_packages(module.__path__, prefix=module.__name__ + "."):
                    self.registry.register_module(importlib.import_module(info.name))
        return self

    def method(self, name: str) -> Callable[[Callable[[Dict[str, Any]], Any]], Callable[[Dict[str, Any]], Any]]:
        """Decorator registering a custom (or overriding a built-in) JSON-RPC method: ``fn(params) -> result``."""

        def wrap(fn: Callable[[Dict[str, Any]], Any]) -> Callable[[Dict[str, Any]], Any]:
            self.processor.register_method(name, fn)
            return fn

        return wrap

    # ---- web application ---------------------------------------------------------------------
    def route(self, path: str, methods: Sequence[str] = ("GET",), require_auth: bool = False) -> Callable[[Any], Any]:
        """Decorator registering a web route served alongside the MCP endpoint.

        ``path`` may contain ``{name}`` placeholders (available as ``request.path_params``). The handler
        receives an :class:`HttpRequest` and may return an :class:`HttpResponse`, ``str`` (HTML),
        ``dict``/``list`` (JSON), ``bytes``, ``None`` (204) or ``(body, status)``; raise ``HttpError`` for errors.
        """

        def wrap(fn: Callable[[HttpRequest], Any]) -> Callable[[HttpRequest], Any]:
            self.app.add_route(path, fn, methods, require_auth)
            return fn

        return wrap

    def static(self, prefix: str, directory: str, index: str = "index.html") -> "McpServer":
        """Serve a directory of static files (HTML/JS/CSS/images) under URL ``prefix`` (e.g. ``"/"``)."""
        self.app.add_static(prefix, directory, index)
        return self

    # ---- server -> client notifications ------------------------------------------------------
    def broadcast(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        """Send a JSON-RPC notification to every connected GET/SSE stream. Returns the receiver count."""
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        return self.sessions.broadcast(message)

    def notify_tools_list_changed(self) -> int:
        return self.broadcast("notifications/tools/list_changed")

    def notify_resources_list_changed(self) -> int:
        return self.broadcast("notifications/resources/list_changed")

    def notify_prompts_list_changed(self) -> int:
        return self.broadcast("notifications/prompts/list_changed")

    # ---- hosting -----------------------------------------------------------------------------
    def handle(self, request: HttpRequest) -> HttpResponse:
        """Process a transport-neutral request (useful for custom hosting or tests)."""
        return self.app.handle(request)

    @property
    def wsgi_app(self) -> Callable[..., Iterable[bytes]]:
        """A PEP 3333 WSGI application for hosting under waitress/gunicorn/etc."""
        return make_wsgi_app(self.app)

    @property
    def asgi_app(self) -> McpAsgiApp:
        """An ASGI 3 application, e.g. ``uvicorn myapp:mcp.asgi_app`` or ``app.mount("/", mcp.asgi_app)``."""
        if self._asgi_app is None:
            self._asgi_app = McpAsgiApp(self.app, self.runner)
        return self._asgi_app

    def asgi_middleware(self, app: Any, include_web_routes: bool = True) -> McpAsgiMiddleware:
        """Wrap any ASGI app (Django ASGI, Quart, Starlette, FastAPI, ...): MCP paths are served by this server."""
        return McpAsgiMiddleware(app, server=self, include_web_routes=include_web_routes)

    def wsgi_middleware(self, app: Any, include_web_routes: bool = True) -> McpWsgiMiddleware:
        """Wrap any WSGI app (Django, Flask, Bottle, Pyramid, ...): MCP paths are served by this server."""
        return McpWsgiMiddleware(app, server=self, include_web_routes=include_web_routes)

    def mount(self, app: Any, include_web_routes: bool = True) -> Any:
        """Attach this MCP server to an existing web application with one line - the Python counterpart of
        ``app.MapMcpHttpServer()`` in the .NET library.

        * **FastAPI / Starlette** (and their routers): registers ``options.endpoint`` (default ``/mcp``),
          the OAuth well-known endpoint when configured, and any ``@mcp.route`` web routes.
        * **Flask**: wraps ``app.wsgi_app`` with :class:`McpWsgiMiddleware`.

        For other frameworks use :meth:`asgi_middleware` / :meth:`wsgi_middleware`. Returns ``app``.
        """
        if hasattr(app, "add_route") and callable(app.add_route) and not hasattr(app, "add_url_rule"):
            asgi = self.asgi_app
            paths = [self.options.endpoint]
            if self.app.serves_oauth_metadata:
                paths += [WELL_KNOWN_OAUTH_PATH, WELL_KNOWN_OAUTH_PATH + "/{rest:path}"]
            if include_web_routes:
                paths += [route.path for route in self.app.routes]
            routes = getattr(getattr(app, "router", app), "routes", None)
            for path in paths:
                # A non-function endpoint is treated by Starlette as a raw ASGI app (all methods allowed).
                app.add_route(path, asgi, include_in_schema=False)
                if isinstance(routes, list) and routes:
                    routes.insert(0, routes.pop())  # take precedence over catch-alls (e.g. a static mount at "/")
            self._log_mount(type(app).__name__, paths)
            return app

        if hasattr(app, "wsgi_app") and hasattr(app, "add_url_rule"):  # Flask
            app.wsgi_app = McpWsgiMiddleware(app.wsgi_app, server=self, include_web_routes=include_web_routes)
            self._log_mount(type(app).__name__, [self.options.endpoint])
            return app

        raise McpConfigurationError(
            f"Don't know how to mount on {type(app).__name__}. Use mcp.asgi_middleware(app) for ASGI apps "
            "or mcp.wsgi_middleware(app) for WSGI apps (e.g. Django)."
        )

    def _log_mount(self, host: str, paths: Sequence[str]) -> None:
        if self.options.enable_logging:
            _ensure_console_logging()
            r = self.registry
            logger.info(
                "[MCP] %s v%s mounted on %s at %s (tools: %d, resources: %d, templates: %d, prompts: %d)",
                self.options.server_name, self.options.server_version, host, ", ".join(paths),
                len(r.tools), len(r.resources), len(r.resource_templates), len(r.prompts),
            )

    def create_http_server(self, host: str = "127.0.0.1", port: int = 8000,
                           ssl_context: Optional[ssl.SSLContext] = None) -> McpThreadingHTTPServer:
        """Create (but don't start) a stdlib threaded HTTP server bound to ``host:port``."""
        return McpThreadingHTTPServer((host, port), self.app, ssl_context)

    def start(self, host: str = "127.0.0.1", port: int = 8000,
              ssl_context: Optional[ssl.SSLContext] = None) -> McpThreadingHTTPServer:
        """Start serving on a background daemon thread and return the server (``port=0`` picks a free port)."""
        if self._http_server is not None:
            raise RuntimeError("Server is already running")
        self._http_server = self.create_http_server(host, port, ssl_context).start_background()
        return self._http_server

    def stop(self) -> None:
        """Stop a server started with :meth:`start` (or :meth:`run`)."""
        server, self._http_server = self._http_server, None
        if server is not None:
            server.stop()
        self.sessions.close_all()
        self.processor.shutdown()
        self.runner.shutdown()

    def run(self, host: str = "127.0.0.1", port: int = 8000, ssl_context: Optional[ssl.SSLContext] = None,
            configure_logging: bool = True) -> None:
        """Serve forever (blocking) until Ctrl+C."""
        if configure_logging:
            _ensure_console_logging()
        server = self.create_http_server(host, port, ssl_context)
        self._http_server = server
        self.print_banner(server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self._http_server = None
            self.sessions.close_all()
            server.server_close()
            self.processor.shutdown()

    def print_banner(self, url: Optional[str] = None) -> None:
        o, r = self.options, self.registry
        lines = [
            "",
            "========================================",
            f"{o.server_name} v{o.server_version}",
            "========================================",
            f"Endpoint: {url or o.endpoint} (Streamable HTTP)",
            f"Protocol Versions: {', '.join(o.supported_protocol_versions)}",
            f"Stateless Mode: {o.stateless_mode}",
            f"Authorization Required: {o.require_authorization}",
            f"Tools: {len(r.tools)}",
            f"Resources: {len(r.resources)} (Templates: {len(r.resource_templates)})",
            f"Prompts: {len(r.prompts)}",
        ]
        if self.app.routes or self.app.static_mounts:
            web_url = url.rsplit(o.endpoint, 1)[0] + "/" if url and url.endswith(o.endpoint) else "/"
            lines.append(f"Web App: {web_url} ({len(self.app.routes)} routes, "
                         f"static: {', '.join(m[0] for m in self.app.static_mounts) or 'none'})")
        lines += [
            f"Library: py-http-mcp-server {__version__}",
            "========================================",
            "",
        ]
        print("\n".join(lines), flush=True)


def _ensure_console_logging() -> None:
    pkg_logger = logging.getLogger("py_http_mcp_server")
    if pkg_logger.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(logging.INFO)
