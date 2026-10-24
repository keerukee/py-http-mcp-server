"""End-to-end tests against a real server on an ephemeral port (mirrors McpServerIntegrationTests.cs)."""

import http.client
import json
import threading
import time
import unittest
from typing import Any, Dict, Optional, Tuple

from tests.handlers import TestHandlers

from py_http_mcp_server import McpIcon, McpServer, OAuthProtectedResourceOptions, bearer_token_authenticator


class _Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def request(self, method: str, path: str = "/mcp", body: Any = None,
                headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = None if body is None else (body if isinstance(body, (bytes, str)) else json.dumps(body))
            hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            hdrs.update(headers or {})
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def post(self, body: Any, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], Any]:
        status, hdrs, raw = self.request("POST", "/mcp", body, headers)
        return status, hdrs, (json.loads(raw) if raw and hdrs.get("content-type", "").startswith("application/json") else raw)


class McpServerIntegrationTests(unittest.TestCase):
    server: McpServer
    client: _Client

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = McpServer(
            enable_logging=False,
            enable_http_logging=False,
            server_title="Test MCP Server",
            website_url="https://mcp.example.com",
            icons=[McpIcon(src="/icon.png", mime_type="image/png")],
            instructions="Use tools to compute values.",
            allowed_origins={"https://app.devin.ai"},
            oauth_protected_resource=OAuthProtectedResourceOptions(
                authorization_servers=["https://auth.example.com"], scopes_supported=["mcp:tools"]
            ),
            sse_keepalive_interval=0.2,
        )
        cls.server.register(TestHandlers)
        http_server = cls.server.start(port=0)
        cls.client = _Client(http_server.port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def test_post_stateless_no_session_id_returns_response(self) -> None:
        status, headers, body = self.client.post({"jsonrpc": "2.0", "method": "ping", "id": 1})
        self.assertEqual(200, status)
        self.assertNotIn("mcp-session-id", headers)
        self.assertIn("mcp-protocol-version", headers)
        self.assertEqual({"jsonrpc": "2.0", "id": 1, "result": {}}, body)

    def test_initialize_negotiates_version_and_generates_session(self) -> None:
        status, headers, body = self.client.post({
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {"protocolVersion": "2026-07-28", "clientInfo": {"name": "devin-mcp-client", "version": "2.0.0"}},
        })
        self.assertEqual(200, status)
        self.assertIn("mcp-session-id", headers)
        self.assertEqual("2025-11-25", headers["mcp-protocol-version"])
        self.assertEqual("2025-11-25", body["result"]["protocolVersion"])
        self.assertEqual("Use tools to compute values.", body["result"]["instructions"])
        self.assertEqual("http://127.0.0.1:%d/icon.png" % self.client.port, body["result"]["serverInfo"]["icons"][0]["src"])

        _, _, older = self.client.post({
            "jsonrpc": "2.0", "method": "initialize", "id": 2,
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "legacy", "version": "1.0.0"}},
        })
        self.assertEqual("2025-06-18", older["result"]["protocolVersion"])

    def test_routing_headers_valid_and_mismatch(self) -> None:
        status, _, body = self.client.post(
            {"jsonrpc": "2.0", "method": "tools/call", "id": 2,
             "params": {"name": "get_weather", "arguments": {"city": "Seattle"}}},
            headers={"Mcp-Method": "tools/call", "Mcp-Name": "get_weather", "MCP-Protocol-Version": "2026-07-28"},
        )
        self.assertEqual(200, status)
        self.assertEqual("Seattle", body["result"]["structuredContent"]["City"])
        self.assertFalse(body["result"]["isError"])

        status, _, _ = self.client.post({"jsonrpc": "2.0", "method": "tools/list", "id": 3}, headers={"Mcp-Method": "prompts/list"})
        self.assertEqual(400, status)

    def test_disallowed_origin_returns_403(self) -> None:
        status, _, _ = self.client.post({"jsonrpc": "2.0", "method": "ping", "id": 1}, headers={"Origin": "https://malicious.example.com"})
        self.assertEqual(403, status)
        status, _, _ = self.client.post({"jsonrpc": "2.0", "method": "ping", "id": 1}, headers={"Origin": "https://app.devin.ai"})
        self.assertEqual(200, status)

    def test_oauth_protected_resource_metadata(self) -> None:
        status, _, raw = self.client.request("GET", "/.well-known/oauth-protected-resource")
        self.assertEqual(200, status)
        doc = json.loads(raw)
        self.assertEqual("https://auth.example.com", doc["authorization_servers"][0])
        self.assertTrue(doc["client_id_metadata_document_supported"])
        self.assertEqual("http://127.0.0.1:%d/mcp" % self.client.port, doc["resource"])

    def test_server_discover(self) -> None:
        status, headers, body = self.client.post({
            "jsonrpc": "2.0", "id": "discover-1", "method": "server/discover",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0.0"},
            }},
        })
        self.assertEqual(200, status)
        self.assertEqual("2026-07-28", headers["mcp-protocol-version"])
        result = body["result"]
        self.assertEqual("discover-1", body["id"])
        self.assertEqual("complete", result["resultType"])
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertEqual("public", result["cacheScope"])
        self.assertEqual(60000, result["ttlMs"])
        self.assertEqual("Use tools to compute values.", result["instructions"])
        self.assertNotIn("protocolVersion", result)
        self.assertNotIn("serverInfo", result)
        self.assertIn("io.modelcontextprotocol/tasks", result["capabilities"]["extensions"])
        info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
        self.assertEqual("Test MCP Server", info["title"])
        self.assertEqual("https://mcp.example.com", info["websiteUrl"])

    def test_unsupported_protocol_version_in_meta(self) -> None:
        status, _, body = self.client.post({
            "jsonrpc": "2.0", "id": 99, "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1999-01-01"}},
        })
        self.assertEqual(200, status)
        self.assertEqual(-32600, body["error"]["code"])

    def test_async_task_extension(self) -> None:
        _, _, body = self.client.post({
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "async_echo", "arguments": {"text": "TaskPayload"}, "task": {"ttl": 30000}},
        })
        created = body["result"]
        self.assertEqual("task", created["resultType"])
        self.assertEqual(30000, created["ttlMs"])
        task_id = created["task"]["taskId"]
        self.assertEqual(task_id, created["taskId"])

        status, final = "working", None
        for i in range(50):
            time.sleep(0.02)
            _, _, got = self.client.post({"jsonrpc": "2.0", "id": 11 + i, "method": "tasks/get", "params": {"taskId": task_id}})
            final = got["result"]
            status = final["status"]
            if status != "working":
                break
        self.assertEqual("completed", status)
        self.assertEqual("Async: TaskPayload", final["result"]["content"][0]["text"])

        _, _, listed = self.client.post({"jsonrpc": "2.0", "id": 30, "method": "tasks/list"})
        self.assertTrue(listed["result"]["tasks"])

        _, _, updated = self.client.post({"jsonrpc": "2.0", "id": 31, "method": "tasks/update",
                                          "params": {"taskId": task_id, "statusMessage": "Updated message"}})
        self.assertEqual("Updated message", updated["result"]["statusMessage"])

    def test_task_cancel(self) -> None:
        _, _, body = self.client.post({
            "jsonrpc": "2.0", "id": 60, "method": "tools/call",
            "params": {"name": "slow_tool", "arguments": {}, "_meta": {"io.modelcontextprotocol/tasks": True}},
        })
        task_id = body["result"]["taskId"]
        _, _, cancelled = self.client.post({"jsonrpc": "2.0", "id": 61, "method": "tasks/cancel", "params": {"taskId": task_id}})
        self.assertEqual("cancelled", cancelled["result"]["status"])
        time.sleep(0.1)
        _, _, got = self.client.post({"jsonrpc": "2.0", "id": 62, "method": "tasks/get", "params": {"taskId": task_id}})
        self.assertEqual("cancelled", got["result"]["status"])

    def test_tool_error_is_error_true_and_rich_content(self) -> None:
        _, _, failed = self.client.post({"jsonrpc": "2.0", "id": 40, "method": "tools/call",
                                         "params": {"name": "failing_tool", "arguments": {}}})
        self.assertTrue(failed["result"]["isError"])
        self.assertIn("Database offline", failed["result"]["content"][0]["text"])

        _, _, rich = self.client.post({"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                                       "params": {"name": "rich_content_tool", "arguments": {}}})
        contents = rich["result"]["content"]
        self.assertEqual(5, len(contents))
        self.assertEqual(["text", "image", "audio", "resource_link", "resource"], [c["type"] for c in contents])
        self.assertEqual("image/png", contents[1]["mimeType"])

    def test_unknown_tool_and_method(self) -> None:
        _, _, body = self.client.post({"jsonrpc": "2.0", "id": 42, "method": "tools/call", "params": {"name": "nope"}})
        self.assertEqual(-32602, body["error"]["code"])
        _, _, body = self.client.post({"jsonrpc": "2.0", "id": 43, "method": "does/not/exist"})
        self.assertEqual(-32601, body["error"]["code"])

    def test_resources_and_prompts_over_http(self) -> None:
        _, _, body = self.client.post({"jsonrpc": "2.0", "id": 70, "method": "resources/read", "params": {"uri": "users://7/profile"}})
        self.assertEqual([{"uri": "users://7/profile", "mimeType": "application/json", "text": '{"id":7,"name":"User_7"}'}],
                         body["result"]["contents"])
        _, _, body = self.client.post({"jsonrpc": "2.0", "id": 71, "method": "prompts/get",
                                       "params": {"name": "greet", "arguments": {"name": "Ada"}}})
        self.assertEqual({"role": "user", "content": {"type": "text", "text": "Hello Ada"}}, body["result"]["messages"][0])

    def test_batch_request(self) -> None:
        _, _, body = self.client.post([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 2, "b": 3}}},
        ])
        self.assertEqual(2, len(body))
        self.assertEqual("5", body[1]["result"]["content"][0]["text"])

    def test_parse_error(self) -> None:
        status, _, body = self.client.post("{not json")
        self.assertEqual(400, status)
        self.assertEqual(-32700, body["error"]["code"])

    def test_sse_only_accept_returns_event_stream(self) -> None:
        status, headers, raw = self.client.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 5, "method": "ping"},
                                                   headers={"Accept": "text/event-stream"})
        self.assertEqual(200, status)
        self.assertEqual("text/event-stream", headers["content-type"])
        self.assertTrue(raw.decode().startswith("id: 1\nevent: message\ndata: {"))

    def test_notification_202_and_unsupported_header_400(self) -> None:
        status, _, raw = self.client.request("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(202, status)
        self.assertEqual(b"", raw)
        status, _, _ = self.client.post({"jsonrpc": "2.0", "id": 50, "method": "ping"}, headers={"MCP-Protocol-Version": "2020-01-01"})
        self.assertEqual(400, status)

    def test_get_opens_sse_stream_and_receives_broadcast(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.client.port, timeout=10)
        try:
            conn.request("GET", "/mcp", headers={"Accept": "text/event-stream"})
            resp = conn.getresponse()
            self.assertEqual(200, resp.status)
            self.assertEqual("text/event-stream", resp.getheader("Content-Type"))
            session_id = resp.getheader("Mcp-Session-Id")
            self.assertTrue(session_id)

            first = resp.readline().decode()
            self.assertEqual("id: 1\n", first)
            self.assertEqual("event: endpoint\n", resp.readline().decode())

            deadline = time.time() + 2
            while len(self.server.sessions) == 0 and time.time() < deadline:
                time.sleep(0.01)
            threading.Timer(0.05, self.server.notify_tools_list_changed).start()
            data = b""
            while b"list_changed" not in data and time.time() < deadline + 2:
                data += resp.readline()
            self.assertIn(b"notifications/tools/list_changed", data)
        finally:
            conn.close()

    def test_delete_with_session_id_returns_ok(self) -> None:
        status, _, _ = self.client.request("DELETE", "/mcp", headers={"Mcp-Session-Id": "test-session-123"})
        self.assertEqual(200, status)

    def test_unknown_path_404_and_bad_method_405(self) -> None:
        self.assertEqual(404, self.client.request("GET", "/other")[0])
        self.assertEqual(405, self.client.request("PUT", "/mcp", {})[0])


class McpServerAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = McpServer(
            enable_logging=False,
            enable_http_logging=False,
            require_authorization=True,
            authenticator=bearer_token_authenticator({"secret-token": {"name": "McpUser"}}),
            use_default_cors=True,
            oauth_protected_resource=OAuthProtectedResourceOptions(scopes_supported=["mcp.tools", "mcp.read"]),
        )

        @cls.server.tool("whoami", "Returns the authenticated principal")
        def whoami() -> str:
            from py_http_mcp_server import get_current_request

            return get_current_request().user["name"]

        cls.client = _Client(cls.server.start(port=0).port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def test_missing_token_returns_401_with_discovery_header(self) -> None:
        status, headers, raw = self.client.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(401, status)
        self.assertIn('resource_metadata="http://127.0.0.1:%d/.well-known/oauth-protected-resource"' % self.client.port,
                      headers["www-authenticate"])
        self.assertIn('scope="mcp.tools mcp.read"', headers["www-authenticate"])
        self.assertEqual("unauthorized", json.loads(raw)["error"])
        self.assertEqual("*", headers["access-control-allow-origin"])

    def test_valid_token_and_principal_visible_to_tools(self) -> None:
        status, _, body = self.client.post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}},
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(200, status)
        self.assertEqual("McpUser", body["result"]["content"][0]["text"])

    def test_preflight_and_well_known_skip_auth(self) -> None:
        status, headers, _ = self.client.request("OPTIONS", "/mcp", headers={"Access-Control-Request-Headers": "content-type"})
        self.assertEqual(204, status)
        self.assertIn("POST", headers["access-control-allow-methods"])
        self.assertEqual(200, self.client.request("GET", "/.well-known/oauth-protected-resource")[0])


if __name__ == "__main__":
    unittest.main()
