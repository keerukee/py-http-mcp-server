"""WSGI adapter (standard library only).

* :func:`make_wsgi_app` - host the MCP server under any WSGI server (waitress, gunicorn, mod_wsgi, ...).
* :class:`McpWsgiMiddleware` - embed it in an existing WSGI app (Flask, Django, Bottle, Pyramid, ...):
  MCP paths are served, everything else passes through.

Note: GET SSE streams hold a worker thread for their lifetime; use a threaded WSGI server.
"""

from http import HTTPStatus
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .exceptions import McpError
from .transport import Headers, HttpRequest, HttpResponse, McpHttpApp

__all__ = ["make_wsgi_app", "McpWsgiMiddleware"]

WSGIApp = Callable[[Dict[str, Any], Callable[..., Any]], Iterable[bytes]]

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization"}


def _status_line(code: int) -> str:
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"{code} Unknown"


def _headers_from_environ(environ: Dict[str, Any]) -> Headers:
    items: List[Tuple[str, str]] = []
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            items.append((key[5:].replace("_", "-").title(), value))
    if environ.get("CONTENT_TYPE"):
        items.append(("Content-Type", environ["CONTENT_TYPE"]))
    if environ.get("CONTENT_LENGTH"):
        items.append(("Content-Length", environ["CONTENT_LENGTH"]))
    return Headers(items)


class _ClosingStream:
    """WSGI iterable that also fires ``HttpResponse.on_close`` when the server closes it."""

    def __init__(self, response: HttpResponse) -> None:
        self._response = response
        self._iter = iter(response.stream or ())

    def __iter__(self) -> "_ClosingStream":
        return self

    def __next__(self) -> bytes:
        return next(self._iter)

    def close(self) -> None:
        if self._response.on_close is not None:
            self._response.on_close()
        close = getattr(self._iter, "close", None)
        if callable(close):
            close()


def make_wsgi_app(app: McpHttpApp) -> WSGIApp:
    """Wrap an :class:`McpHttpApp` as a PEP 3333 WSGI application."""

    def wsgi_app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > app.options.max_request_body_bytes:
            response = HttpResponse.json({"jsonrpc": "2.0", "id": None, "error": {
                "code": McpError.INVALID_REQUEST, "message": "Request body too large"}}, 413)
        else:
            body = environ["wsgi.input"].read(length) if length > 0 else b""
            request = HttpRequest(
                method=environ.get("REQUEST_METHOD", "GET"),
                path=environ.get("PATH_INFO") or "/",
                query_string=environ.get("QUERY_STRING", ""),
                headers=_headers_from_environ(environ),
                body=body,
                scheme=environ.get("wsgi.url_scheme", "http"),
                remote_addr=environ.get("REMOTE_ADDR"),
                root_path=environ.get("SCRIPT_NAME", "") or "",
            )
            response = app.handle(request)

        headers = [(k, v) for k, v in response.headers if k.lower() not in _HOP_BY_HOP]
        if response.stream is not None:
            start_response(_status_line(response.status), headers)
            return _ClosingStream(response)  # WSGI servers call .close() when the client leaves
        body_bytes = response.body or b""
        if response.status not in (204, 304):
            headers.append(("Content-Length", str(len(body_bytes))))
        start_response(_status_line(response.status), headers)
        return [body_bytes] if environ.get("REQUEST_METHOD") != "HEAD" else [b""]

    return wsgi_app


class McpWsgiMiddleware:
    """Embed an MCP server in an existing WSGI application.

    Flask::

        app.wsgi_app = McpWsgiMiddleware(app.wsgi_app, server=mcp)

    Django (``wsgi.py``)::

        application = McpWsgiMiddleware(get_wsgi_application(), server=mcp)
    """

    def __init__(self, app: WSGIApp, server: Any, include_web_routes: bool = True) -> None:
        self.app = app
        mcp_app: McpHttpApp = server.app if hasattr(server, "app") else server
        self._mcp_app = mcp_app
        self._mcp = make_wsgi_app(mcp_app)
        self.include_web_routes = include_web_routes

    def __call__(self, environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        if self._mcp_app.is_mcp_path(environ.get("PATH_INFO") or "/", self.include_web_routes):
            return self._mcp(environ, start_response)
        return self.app(environ, start_response)
