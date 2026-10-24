# py-http-mcp-server

A **zero-dependency** Python library for building **Model Context Protocol (MCP)** servers over the
**Streamable HTTP** transport. It's a Python port of the .NET `McpHttpServer` library and uses **only the standard library**.

- Supports MCP revisions `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25` and `2026-07-28`
- Requires Python **3.9+**, with **no runtime dependencies** (`dependencies = []`)
- Tested against the official MCP Python SDK client; should work with MCP Inspector, Claude, Cursor and Devin

---

## Features

| Area | What you get |
|---|---|
| **Transport** | Streamable HTTP `POST` (JSON or SSE replies), `GET` SSE stream, `DELETE` session termination, JSON-RPC batches, `202 Accepted` for notifications |
| **Versioning** | Negotiation via `initialize`, `server/discover` and per-request `_meta`; `MCP-Protocol-Version` header validation |
| **2026-07-28** | `server/discover`, stateless mode, `Mcp-Method` / `Mcp-Name` routing-header validation (SEP-2243), `ttlMs` / `cacheScope` caching hints |
| **Tasks** (SEP-2663) | Background tool execution through `params.task` or `_meta["io.modelcontextprotocol/tasks"]`, plus `tasks/get`, `tasks/list`, `tasks/update` and `tasks/cancel` |
| **Tools** | JSON Schema built from type hints, annotations (`readOnlyHint` and the other hints), `outputSchema` + `structuredContent`, icons, rich content, and `isError` results |
| **Resources** | Static resources plus URI templates (`users://{id}/profile`) whose variables bind to typed parameters; text or binary (`blob`) |
| **Prompts** | Typed arguments; return a string, an `McpContent`, or a list of messages |
| **Async** | Handlers can be `def` or `async def`. A `CancellationToken` is injected automatically, and `notifications/cancelled` is honored |
| **Security** | `authenticator` hook, `401` with an RFC 9728 `WWW-Authenticate` challenge, `/.well-known/oauth-protected-resource`, `allowed_origins` → `403`, optional CORS |
| **Hosting** | Built-in threaded `http.server` (with TLS through `ssl_context`), **ASGI** (`uvicorn`/`hypercorn`), **WSGI** (`waitress`/`gunicorn`) |
| **Framework integration** | `mcp.mount(app)` for **FastAPI / Starlette / Flask**; ASGI and WSGI middleware for **Django**, Quart, Bottle and others. None of these frameworks are imported by the library |
| **Server push** | `server.broadcast(...)` / `notify_tools_list_changed()` to every connected `GET` stream |

---

## Installation

```bash
pip install py-http-mcp-server      # no dependencies are pulled in
```

---

## Minimal code: three ways to host

### 1. Add MCP to an existing FastAPI app (one line)

```python
from fastapi import FastAPI
from py_http_mcp_server import McpServer

app = FastAPI()
mcp = McpServer(server_name="orders-mcp")

@mcp.tool()
def get_order(order_id: int) -> dict:
    """Returns an order by id."""
    return {"id": order_id, "status": "shipped"}

mcp.mount(app)          # ← serves /mcp alongside your API (like app.MapMcpHttpServer() in .NET)
```

Run it with `uvicorn main:app`; the MCP URL is `http://127.0.0.1:8000/mcp`.
- `async def` tools run on FastAPI's own event loop, so they can share your async DB pools and HTTP clients.
- Sync tools run on worker threads.
- `/mcp` is left out of the OpenAPI docs.

### 2. Add MCP to Flask / Django / any ASGI or WSGI app

```python
mcp.mount(flask_app)                                      # Flask: wraps flask_app.wsgi_app

application = mcp.wsgi_middleware(get_wsgi_application()) # Django (wsgi.py)
application = mcp.asgi_middleware(get_asgi_application()) # Django (asgi.py), Quart, ...
app.add_middleware(McpAsgiMiddleware, server=mcp)         # Starlette/FastAPI alternative to mount()
```

The middleware handles only `/mcp` (plus `/.well-known/oauth-protected-resource` when OAuth is configured). Every other request goes to your app.

### 3. Standalone (no framework at all)

```python
mcp.run(port=8000)                 # stdlib threaded HTTP server
# or: uvicorn main:mcp.asgi_app    /   waitress-serve main:mcp.wsgi_app
```

### Organizing tools across modules (like .NET assembly scanning)

```python
# myapp/tools/orders.py
from py_http_mcp_server import mcp_tool

@mcp_tool
def cancel_order(order_id: int) -> str:
    """Cancels an order."""
    ...

# main.py
mcp = McpServer(server_name="orders-mcp").discover("myapp.tools")   # scans the package recursively
mcp.mount(app)
```

`discover()` registers every module-level `@mcp_tool`, `@mcp_resource` and `@mcp_prompt` function and every `@mcp_handler` class it finds.

---

## Quick start (full feature tour)

```python
from dataclasses import dataclass
from typing import Annotated
from py_http_mcp_server import McpServer, Param, CancellationToken

server = McpServer(
    server_name="MyMcpServer",
    server_version="1.0.0",
    instructions="Use these tools to query system metrics and data.",
    use_default_cors=True,
    list_cache_ttl_ms=60000,
)

@server.tool("calculate_sum", "Calculates the sum of two numbers.", read_only_hint=True, idempotent_hint=True)
def calculate_sum(a: Annotated[int, Param("First number")], b: Annotated[int, Param("Second number")]) -> int:
    return a + b

@dataclass
class WeatherReport:
    city: str
    temperature_c: float
    condition: str

@server.tool("get_weather", "Returns structured weather data.", use_structured_output=True)
async def get_weather(city: Annotated[str, Param("City", enum=["Seattle", "London"])], ct: CancellationToken) -> WeatherReport:
    return WeatherReport(city, 24.5, "Sunny")

@server.resource("system://status", "System Status", "Current status", "application/json")
def status() -> dict:
    return {"status": "online"}

@server.resource_template("users://{id}/profile", "User Profile", "Profile by ID", "application/json")
def profile(id: int) -> dict:
    return {"id": id, "role": "developer"}

@server.prompt("greet_user", "Generates a greeting prompt.")
def greet(username: str) -> str:
    return f"Hello {username}, how can I help you today?"

server.run(host="127.0.0.1", port=5000)   # → http://127.0.0.1:5000/mcp
```

If you leave out `name` or `description`, the function name and docstring are used.

### Handler classes (like `[McpHandler]` in .NET)

```python
from py_http_mcp_server import mcp_handler, mcp_tool, mcp_icon, mcp_resource, mcp_prompt

@mcp_handler
class MyAiTools:
    def __init__(self):            # register(MyAiTools) instantiates with no args;
        self.db = ...              # use register(MyAiTools(db)) to inject dependencies

    @mcp_tool("lookup", "Looks up a record.", title="Lookup", read_only_hint=True, icon_src="/icons/lookup.svg")
    @mcp_icon("/icons/lookup-dark.png", "image/png", "dark", "48x48")
    def lookup(self, key: str) -> dict: ...

server.register(MyAiTools)        # also accepts instances, modules, or decorated functions
```

### .NET → Python mapping

| .NET | Python |
|---|---|
| `builder.Services.AddMcpHttpServer(o => ...)` | `McpServer(McpServerOptions(...))` or `McpServer(**options)` |
| `app.MapMcpHttpServer()` (scan entry assembly) | `mcp.discover()` (caller's module) or `mcp.discover("pkg")`, then `mcp.mount(app)` |
| `app.MapMcpHttpServer(typeof(X))` | `mcp.register(X)` + `mcp.mount(app)` (or `mcp.run()` standalone) |
| `[McpHandler]`, `[McpTool]`, `[McpResource]`, `[McpResourceTemplate]`, `[McpPrompt]`, `[McpIcon]` | `@mcp_handler`, `@mcp_tool`, `@mcp_resource`, `@mcp_resource_template`, `@mcp_prompt`, `@mcp_icon` |
| `[McpParameter("desc", EnumValues=...)]` / `[McpArgument]` | `Annotated[T, Param("desc", enum=[...])]` or `params={"x": "desc"}` |
| `CancellationToken ct` | `ct: CancellationToken` (`ct.is_cancelled`, `ct.wait(s)`, `ct.raise_if_cancelled()`) |
| `McpContent.CreateText/CreateImage/...` | `McpContent.create_text/create_image/...` |
| `McpToolResult.Success/Structured/Error` | `McpToolResult.success/structured/error` |
| `RequireAuthorization` + ASP.NET auth policy | `require_authorization=True` + `authenticator=callable(request)` |
| `IHttpContextAccessor` | `get_current_request()` (headers, `user`, `base_url`, `session_id`) |

### Supported parameter types

`str`, `int`, `float`, `bool`, `Decimal`, `datetime`/`date`/`time`, `UUID`, `bytes` (base64), `Enum` (by name, case-insensitive),
`Literal[...]`, `Optional[T]` / `T | None`, `list[T]` / `tuple` / `set`, `dict[str, T]`, dataclasses, `TypedDict`, `NamedTuple`.
Argument names match case-insensitively, and numeric or boolean strings are converted to the declared type (as in .NET).

---

## Advanced

### Rich content

```python
from py_http_mcp_server import McpContent, McpToolResult

@server.tool("generate_chart", "Returns a chart image and resource link.")
def generate_chart(metric: str) -> list:
    return [
        McpContent.create_text(f"Generated chart for {metric}"),
        McpContent.create_image("iVBORw0KGgo...", "image/png"),
        McpContent.create_resource_link("system://status", "System Status", "Live status JSON", "application/json"),
    ]
```

Return values are mapped like this:
- `McpToolResult` → sent as-is
- `McpContent` or a list of them → `content`
- dict, dataclass, NamedTuple or other objects → JSON text **plus** `structuredContent`
- anything else → text

Exceptions become `isError: true` tool results, so the LLM can self-correct.

### Authorization & OAuth discovery

```python
from py_http_mcp_server import McpServer, OAuthProtectedResourceOptions, bearer_token_authenticator

server = McpServer(
    require_authorization=True,
    authenticator=bearer_token_authenticator({"secret-token": {"name": "McpUser"}}),  # or any callable(request) -> principal
    allowed_origins={"https://app.devin.ai"},
    oauth_protected_resource=OAuthProtectedResourceOptions(
        authorization_servers=["https://login.microsoftonline.com/<tenant-id>/v2.0"],
        scopes_supported=["api://my-mcp-server/mcp.tools"],
    ),
)
```

An unauthenticated request gets `401` with `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource", scope="..."`.
CORS preflight and the well-known endpoint skip authentication.

### Async tasks

A client asks for background execution with `{"name": "...", "arguments": {...}, "task": {"ttl": 30000}}`. The server replies at once
with `resultType: "task"` and a `taskId`. The client then polls `tasks/get` and can stop the work with `tasks/cancel`, which fires the tool's `CancellationToken`.
Finished tasks are evicted after their `ttl`.

### Hosting options

```python
server.run(port=5000)                                  # blocking, Ctrl+C to stop
srv = server.start(port=0); print(srv.url); server.stop()  # background thread (tests)
server.run(port=443, ssl_context=my_ssl_context)       # HTTPS
application = server.wsgi_app                          # e.g. `waitress-serve module:application`
application = server.asgi_app                          # e.g. `uvicorn module:application`
```

When the app is mounted under a prefix (ASGI `root_path` or WSGI `SCRIPT_NAME`), the prefix is respected for generated URLs.
Under uvicorn, open MCP `GET` streams are closed when the server shuts down, so Ctrl+C doesn't hang on "Waiting for connections to close".

### Server → client notifications

```python
server.notify_tools_list_changed()
server.broadcast("notifications/message", {"level": "info", "data": "hello"})
```

### Web app + MCP server on one port

The same server can host a regular web application next to the MCP endpoint, so a browser UI and AI agents can share
state. Routes are matched after `/mcp` and the OAuth well-known path, and static files are served last.

```python
from py_http_mcp_server import HttpError, HttpRequest, SessionManager, sse_response

ui_events = SessionManager()

@server.route("/api/notes/{id}", methods=["GET", "DELETE"])
def note(request: HttpRequest):
    note_id = int(request.path_params["id"])        # also: request.query, request.json(), request.headers
    if note_id not in notes:
        raise HttpError(404, "Note not found")       # → 404 {"error": "Note not found"}
    return notes[note_id]                            # dict/list → JSON, str → HTML, None → 204, (body, 201)

@server.route("/api/events")                         # browser EventSource stream
def events(request: HttpRequest):
    return sse_response(ui_events)                   # push with ui_events.broadcast({...})

server.static("/", "web")                            # index.html, JS, CSS (path traversal is blocked)
```

Use `require_auth=True` on a route to apply the server's `authenticator`. Web routes don't require auth by default, even when the MCP endpoint does.

**Demo:** [`examples/sample_server.py`](https://github.com/keerukee/py-http-mcp-server/blob/main/examples/sample_server.py) serves a single-page UI at `http://127.0.0.1:5000/` with three parts:
- a **notes board**, shared between the REST API and the MCP tools `add_note`, `list_notes`, `complete_note`, `update_note` and `delete_note`
- an **MCP playground** that calls `/mcp` from the browser
- a live **activity feed**

Ask your agent to *"add a note to review the release checklist"* and the note appears in the browser right away.

### Custom JSON-RPC methods

```python
@server.method("completion/complete")       # override a built-in or add your own
def complete(params: dict) -> dict:
    return {"completion": {"values": ["Seattle"], "total": 1, "hasMore": False}}
```

---

## Options

All the `McpServerOptions` fields from .NET are present in snake_case (`server_name`, `server_title`, `website_url`, `icons`,
`protocol_version`, `supported_protocol_versions`, `instructions`, `enable_logging`, `enable_http_logging`, `endpoint`,
`require_authorization`, `use_default_cors`, `stateless_mode`, `validate_protocol_version_header`, `validate_routing_headers`,
`list_cache_ttl_ms`, `cache_scope`, `enable_tasks_extension`, `allowed_origins`, `oauth_protected_resource`). Python adds:

| Option | Default | Purpose |
|---|---|---|
| `authenticator` | `None` | `callable(request) -> principal`. A falsy return gives `401`; an `HttpResponse` return is sent as-is |
| `max_request_body_bytes` | 4 MiB | Larger bodies get `413` |
| `sse_keepalive_interval` | 15 s | Keep-alive comments on `GET` streams; also how dead clients are detected |
| `sse_endpoint_event` | `True` | Send the legacy `endpoint` event when a `GET` stream opens (.NET parity) |
| `task_max_workers` | 8 | Thread pool size for background tasks |

## Intentional differences from the .NET library

- `outputSchema` is only emitted for object-shaped return types, as the MCP spec requires, and its property names match `structuredContent`.
- Lists of plain values come back as JSON text without `structuredContent`, because the spec requires `structuredContent` to be an object.
- Malformed JSON gets HTTP `400`, and an empty batch `[]` gets `Invalid Request`.
- A task cancelled while running stays `cancelled`; a late result never overwrites it.
- `prompts/get` returns the prompt's description rather than its name.
- Resources may return `bytes`, which are sent as a base64 `blob`.

## Running the tests

```bash
python -m unittest discover -s tests -t .
```

## Connecting

- **MCP Inspector**: run `npx @modelcontextprotocol/inspector`, choose **Streamable HTTP**, and connect to `http://localhost:5000/mcp`
- **Claude Code**: `claude mcp add --transport http my-server http://localhost:5000/mcp`
- **Devin / Cursor**: set the MCP server URL to `http://<host>:<port>/mcp`
