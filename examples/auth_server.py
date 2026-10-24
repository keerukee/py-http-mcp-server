"""MCP server protected by bearer-token auth with RFC 9728 OAuth discovery (port of the .NET auth sample).

Run:   python examples/auth_server.py
Test:  curl -i -X POST http://127.0.0.1:5000/mcp -H "Content-Type: application/json" \
            -H "Authorization: Bearer secret-token" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from py_http_mcp_server import (  # noqa: E402
    McpServer,
    OAuthProtectedResourceOptions,
    bearer_token_authenticator,
    get_current_request,
)

server = McpServer(
    server_name="SecureMcpServer",
    require_authorization=True,
    # Any callable(request) -> principal works here (validate JWTs, API keys, ...).
    authenticator=bearer_token_authenticator({"secret-token": {"name": "McpUser"}}),
    use_default_cors=True,
    allowed_origins={"http://localhost:6274", "https://app.devin.ai"},
    oauth_protected_resource=OAuthProtectedResourceOptions(
        authorization_servers=["https://login.microsoftonline.com/<tenant-id>/v2.0"],
        scopes_supported=["api://my-mcp-server/mcp.tools"],
    ),
)


@server.tool("whoami", "Returns the authenticated user name.", read_only_hint=True)
def whoami() -> str:
    request = get_current_request()
    return request.user["name"] if request and request.user else "anonymous"


if __name__ == "__main__":
    server.run(port=int(os.environ.get("PORT", "5000")))
