"""Zero-dependency HTTP server built on :mod:`http.server` (``ThreadingHTTPServer``)."""

import logging
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import unquote, urlsplit

from ._version import __version__
from .exceptions import McpError
from .transport import Headers, HttpRequest, HttpResponse, McpHttpApp

__all__ = ["McpThreadingHTTPServer", "McpRequestHandler"]

logger = logging.getLogger("py_http_mcp_server")

_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout, ssl.SSLError, OSError)
_NO_BODY_STATUSES = {204, 304}


class _BodyTooLarge(Exception):
    pass


class McpRequestHandler(BaseHTTPRequestHandler):
    """Translates ``http.server`` requests into :class:`HttpRequest` objects for :class:`McpHttpApp`."""

    protocol_version = "HTTP/1.1"
    server_version = f"py-http-mcp-server/{__version__}"
    server: "McpThreadingHTTPServer"

    def do_GET(self) -> None:  # noqa: N802 - http.server naming convention
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    # ---- body reading ------------------------------------------------------------------------
    def _read_body(self) -> bytes:
        limit = self.server.mcp_app.options.max_request_body_bytes
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, total = [], 0
            while True:
                size_line = self.rfile.readline(65537)
                size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass  # discard trailers
                    break
                total += size
                if total > limit:
                    raise _BodyTooLarge()
                chunks.append(self.rfile.read(size))
                self.rfile.readline(65537)  # CRLF after each chunk
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise _BodyTooLarge()
        return self.rfile.read(length) if length > 0 else b""

    # ---- main dispatch -----------------------------------------------------------------------
    def _handle(self) -> None:
        app = self.server.mcp_app
        try:
            body = self._read_body()
        except _BodyTooLarge:
            self.close_connection = True
            self._write(HttpResponse.json({"jsonrpc": "2.0", "id": None, "error": {
                "code": McpError.INVALID_REQUEST, "message": "Request body too large"}}, 413))
            return
        except (ValueError, *_DISCONNECT_ERRORS):
            self.close_connection = True
            return

        url = urlsplit(self.path)
        request = HttpRequest(
            method=self.command,
            path=unquote(url.path) or "/",
            query_string=url.query,
            headers=Headers(self.headers.items()),
            body=body,
            scheme="https" if isinstance(self.connection, ssl.SSLSocket) else "http",
            remote_addr=self.client_address[0] if self.client_address else None,
        )
        response = app.handle(request)
        self._write(response)

    def _write(self, response: HttpResponse) -> None:
        try:
            self.send_response(response.status)
            for key, value in response.headers:
                self.send_header(key, value)
            if response.stream is not None:
                self._write_stream(response)
                return
            body = response.body or b""
            if response.status not in _NO_BODY_STATUSES:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD" and response.status not in _NO_BODY_STATUSES:
                self.wfile.write(body)
            self.wfile.flush()
        except _DISCONNECT_ERRORS:
            self.close_connection = True

    def _write_stream(self, response: HttpResponse) -> None:
        stream = response.stream
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.close_connection = True
        try:
            self.end_headers()
            self.wfile.flush()
            for chunk in stream:  # type: ignore[union-attr]
                if not chunk:
                    continue
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except _DISCONNECT_ERRORS:
            pass
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature from base class
        logger.debug("%s - %s", self.address_string(), format % args)


class McpThreadingHTTPServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` serving an :class:`McpHttpApp` (one thread per connection)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int], app: McpHttpApp,
                 ssl_context: Optional[ssl.SSLContext] = None) -> None:
        self.mcp_app = app
        self.ssl_context = ssl_context
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, McpRequestHandler)
        if ssl_context is not None:
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True)
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        host = self.server_address[0]
        scheme = "https" if self.ssl_context is not None else "http"
        host = f"[{host}]" if ":" in host else host
        return f"{scheme}://{host}:{self.port}{self.mcp_app.options.endpoint}"

    def start_background(self) -> "McpThreadingHTTPServer":
        """Serve on a daemon thread and return immediately."""
        self._thread = threading.Thread(target=self.serve_forever, name="mcp-http-server", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Close SSE streams, stop serving and release the socket."""
        self.mcp_app.sessions.close_all()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
