"""ASGI adapter (standard library only).

Lets the MCP server run under any ASGI server (uvicorn, hypercorn, daphne) or be embedded in any ASGI
framework (FastAPI, Starlette, Quart, Django ASGI, ...) without importing those frameworks.

* :class:`McpAsgiApp` - an ASGI 3 application serving the MCP endpoint (and the server's web routes).
* :class:`McpAsgiMiddleware` - wraps an existing ASGI app; MCP paths are served, everything else passes through.

Request handling runs on a worker thread so the host event loop is never blocked; ``async def`` tools run
on the host's loop (see :meth:`AsyncRunner.bind_loop`), so they can share the app's async resources.
"""

import asyncio
import concurrent.futures
import contextvars
import logging
import sys
import threading
import weakref
from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional

from ._async import AsyncRunner
from .exceptions import McpError
from .transport import Headers, HttpRequest, HttpResponse, McpHttpApp

__all__ = ["McpAsgiApp", "McpAsgiMiddleware"]

logger = logging.getLogger("py_http_mcp_server")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade"}
_DONE = object()


def _route_path(scope: Scope) -> str:
    """Path relative to the mount point (handles both 'full path + root_path' and pre-stripped styles)."""
    path = scope.get("path") or "/"
    root = scope.get("root_path") or ""
    if root and path.startswith(root) and (len(path) == len(root) or path[len(root)] == "/"):
        return path[len(root):] or "/"
    return path


# ---------------------------------------------------------------------------------------------
# Best-effort graceful shutdown for uvicorn: long-lived SSE streams would otherwise keep
# "Waiting for connections to close" forever on Ctrl+C.
# ---------------------------------------------------------------------------------------------
_shutdown_callbacks: "weakref.WeakSet[McpHttpApp]" = weakref.WeakSet()
_hook_lock = threading.Lock()


def _install_uvicorn_exit_hook(app: McpHttpApp) -> None:
    _shutdown_callbacks.add(app)
    module = sys.modules.get("uvicorn.server")
    server_cls = getattr(module, "Server", None) if module else None
    if server_cls is None:
        return
    with _hook_lock:
        original = server_cls.handle_exit
        if getattr(original, "_py_http_mcp_patched", False):
            return

        def handle_exit(self: Any, sig: int, frame: Any) -> None:
            for mcp_app in list(_shutdown_callbacks):
                mcp_app.sessions.close_all()
            original(self, sig, frame)

        handle_exit._py_http_mcp_patched = True  # type: ignore[attr-defined]
        server_cls.handle_exit = handle_exit


class McpAsgiApp:
    """ASGI 3 application for an :class:`McpHttpApp`.

    :param fallback: optional ASGI app that receives every request this app does not own (middleware mode).
    :param include_web_routes: in middleware mode, also claim routes registered with ``server.route``.
    """

    def __init__(
        self,
        app: McpHttpApp,
        runner: Optional[AsyncRunner] = None,
        fallback: Optional[ASGIApp] = None,
        include_web_routes: bool = True,
        max_workers: int = 32,
    ) -> None:
        self.app = app
        self.runner = runner
        self.fallback = fallback
        self.include_web_routes = include_web_routes
        self._max_workers = max_workers
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()
        self._hooked = False

    # ---- ASGI entry point ----------------------------------------------------------------------
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if not self._hooked:
            self._hooked = True
            _install_uvicorn_exit_hook(self.app)

        if scope_type == "http":
            path = _route_path(scope)
            if self.fallback is not None and not self.app.is_mcp_path(path, self.include_web_routes):
                await self.fallback(scope, receive, send)
                return
            await self._handle_http(scope, receive, send, path)
            return

        if scope_type == "lifespan":
            if self.fallback is not None:
                await self.fallback(scope, self._closing_receive(receive), send)
            else:
                await self._lifespan(receive, send)
            return

        if self.fallback is not None:
            await self.fallback(scope, receive, send)
        elif scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1000})

    def _closing_receive(self, receive: Receive) -> Receive:
        async def wrapped() -> Message:
            message = await receive()
            if message.get("type") == "lifespan.shutdown":
                self.app.sessions.close_all()
            return message

        return wrapped

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                self.app.sessions.close_all()
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ---- HTTP ------------------------------------------------------------------------------------
    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self._max_workers, thread_name_prefix="mcp-asgi"
                )
            return self._executor

    async def _handle_http(self, scope: Scope, receive: Receive, send: Send, path: str) -> None:
        loop = asyncio.get_running_loop()
        if self.runner is not None:
            self.runner.bind_loop(loop)

        limit = self.app.options.max_request_body_bytes
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > limit:
                await self._send_response(send, scope, HttpResponse.json(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": McpError.INVALID_REQUEST, "message": "Request body too large"}},
                    413,
                ))
                return
            if not message.get("more_body", False):
                break

        client = scope.get("client")
        request = HttpRequest(
            method=scope.get("method", "GET"),
            path=path,
            headers=Headers((k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])),
            body=bytes(body),
            query_string=(scope.get("query_string") or b"").decode("latin-1"),
            scheme=scope.get("scheme", "http"),
            remote_addr=client[0] if client else None,
            root_path=scope.get("root_path", "") or "",
        )
        ctx = contextvars.copy_context()
        response = await loop.run_in_executor(self._get_executor(), ctx.run, self.app.handle, request)

        if response.stream is None:
            await self._send_response(send, scope, response)
        else:
            await self._send_stream(send, receive, response, loop)

    @staticmethod
    def _encode_headers(response: HttpResponse) -> List[List[bytes]]:
        return [
            [k.lower().encode("latin-1"), v.encode("latin-1")]
            for k, v in response.headers if k.lower() not in _HOP_BY_HOP
        ]

    async def _send_response(self, send: Send, scope: Scope, response: HttpResponse) -> None:
        headers = self._encode_headers(response)
        body = response.body or b""
        if response.status not in (204, 304):
            headers.append([b"content-length", str(len(body)).encode("ascii")])
        await send({"type": "http.response.start", "status": response.status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})

    async def _send_stream(self, send: Send, receive: Receive, response: HttpResponse,
                           loop: asyncio.AbstractEventLoop) -> None:
        queue: "asyncio.Queue[Any]" = asyncio.Queue()
        stop = threading.Event()
        stream = response.stream

        def _put(item: Any) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # loop closed
                stop.set()

        def _pump() -> None:  # dedicated thread: SSE generators block between events
            try:
                for chunk in stream:  # type: ignore[union-attr]
                    if stop.is_set():
                        break
                    if chunk:
                        _put(chunk)
            except Exception:  # noqa: BLE001
                logger.exception("SSE stream failed")
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                _put(_DONE)

        async def _wait_disconnect() -> None:
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    return

        await send({"type": "http.response.start", "status": response.status, "headers": self._encode_headers(response)})
        threading.Thread(target=_pump, name="mcp-asgi-stream", daemon=True).start()
        watcher = asyncio.ensure_future(_wait_disconnect())
        disconnected = False
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    disconnected = True
                    break
                chunk = getter.result()
                if chunk is _DONE:
                    break
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if not disconnected:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:  # noqa: BLE001 - client went away mid-stream (CancelledError still propagates)
            pass
        finally:
            stop.set()
            watcher.cancel()
            if response.on_close is not None:
                response.on_close()


class McpAsgiMiddleware:
    """Embed an MCP server in an existing ASGI app.

    Works with Starlette/FastAPI's ``add_middleware``::

        app.add_middleware(McpAsgiMiddleware, server=mcp)

    or by wrapping any ASGI callable (Django, Quart, ...)::

        application = McpAsgiMiddleware(django_asgi_app, server=mcp)
    """

    def __init__(self, app: ASGIApp, server: Any, include_web_routes: bool = True) -> None:
        self.app = app
        self._mcp = McpAsgiApp(server.app, getattr(server, "runner", None), fallback=app,
                               include_web_routes=include_web_routes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._mcp(scope, receive, send)
