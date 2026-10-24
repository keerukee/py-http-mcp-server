"""Add an MCP server to an existing Flask app with ONE line: ``mcp.mount(app)``.

Run:   pip install flask && python examples/flask_app.py
Web:   http://127.0.0.1:8001/
MCP:   http://127.0.0.1:8001/mcp
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))  # repo-only shim

from flask import Flask, jsonify  # noqa: E402

from py_http_mcp_server import McpServer  # noqa: E402

app = Flask(__name__)
mcp = McpServer(server_name="flask-mcp")

TODOS = ["Write docs", "Ship release"]


@app.get("/")
def index():
    return jsonify(todos=TODOS)


@mcp.tool(read_only_hint=True)
def list_todos() -> dict:
    """Lists the to-do items."""
    return {"todos": TODOS}


@mcp.tool()
def add_todo(text: str) -> str:
    """Adds a to-do item."""
    TODOS.append(text)
    return f"Added: {text}"


mcp.mount(app)  # <- wraps app.wsgi_app; /mcp is served by the MCP server, everything else by Flask

if __name__ == "__main__":
    app.run(port=8001, threaded=True)  # threaded=True so MCP GET streams don't block other requests
