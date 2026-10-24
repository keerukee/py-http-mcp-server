# Changelog

## 0.1.0 (unreleased)

First public release.

- Zero-dependency MCP server over Streamable HTTP (protocol revisions 2024-11-05 through 2026-07-28)
- Tools, resources, resource templates and prompts built from Python type hints; `async def` support; `CancellationToken`
- Tasks extension (SEP-2663), `server/discover`, routing-header validation, OAuth protected-resource discovery, origin checks
- Hosting:
  - standalone stdlib server
  - ASGI (`uvicorn`) and WSGI (`waitress`, `gunicorn`)
  - `mcp.mount(app)` for FastAPI, Starlette and Flask
  - ASGI and WSGI middleware for Django and other frameworks
- Web routes and static files, so one server can be both a web app and an MCP server
