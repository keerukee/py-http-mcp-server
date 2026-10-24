import http.client
import json
import os
import shutil
import tempfile
import time
import unittest

from py_http_mcp_server import HttpError, HttpRequest, McpConfigurationError, McpServer, SessionManager, sse_response


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.static_dir = tempfile.mkdtemp()
        with open(os.path.join(cls.static_dir, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<h1>home</h1>")
        os.makedirs(os.path.join(cls.static_dir, "js"))
        with open(os.path.join(cls.static_dir, "js", "app.js"), "w", encoding="utf-8") as fh:
            fh.write("console.log(1)")
        cls.secret = os.path.join(os.path.dirname(cls.static_dir), "secret_outside_root.txt")
        with open(cls.secret, "w", encoding="utf-8") as fh:
            fh.write("top secret")

        server = McpServer(enable_logging=False, enable_http_logging=False)
        cls.hub = SessionManager()
        items = {1: {"id": 1, "name": "first"}}

        @server.tool("ping_tool", "MCP still works")
        def ping_tool() -> str:
            return "pong"

        @server.route("/api/items", methods=["GET"])
        def list_items(request: HttpRequest):
            return list(items.values())

        @server.route("/api/items", methods=["POST"])
        def create_item(request: HttpRequest):
            data = request.json()
            item = {"id": len(items) + 1, "name": data["name"]}
            items[item["id"]] = item
            return item, 201

        @server.route("/api/items/{item_id}", methods=["GET", "DELETE"])
        def item(request: HttpRequest):
            item_id = int(request.path_params["item_id"])
            if item_id not in items:
                raise HttpError(404, f"Item {item_id} not found")
            if request.method == "DELETE":
                del items[item_id]
                return None
            return items[item_id]

        @server.route("/hello")
        def hello(request: HttpRequest):
            return f"<p>Hello {request.query.get('name', 'world')}</p>"

        @server.route("/events")
        def events(request: HttpRequest):
            return sse_response(cls.hub, keepalive_interval=0.2, initial_event=("ready", "{}"))

        server.static("/", cls.static_dir)
        cls.server = server
        cls.port = server.start(port=0).port

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        shutil.rmtree(cls.static_dir, ignore_errors=True)
        os.remove(cls.secret)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def test_json_routes_and_path_params(self) -> None:
        status, headers, raw = self.request("GET", "/api/items")
        self.assertEqual(200, status)
        self.assertEqual("application/json", headers["content-type"])
        self.assertEqual("first", json.loads(raw)[0]["name"])

        status, _, raw = self.request("POST", "/api/items", {"name": "second"})
        self.assertEqual(201, status)
        new_id = json.loads(raw)["id"]
        self.assertEqual("second", json.loads(self.request("GET", f"/api/items/{new_id}")[2])["name"])
        self.assertEqual(204, self.request("DELETE", f"/api/items/{new_id}")[0])

    def test_http_error_and_405(self) -> None:
        status, _, raw = self.request("GET", "/api/items/999")
        self.assertEqual(404, status)
        self.assertEqual({"error": "Item 999 not found"}, json.loads(raw))
        status, headers, _ = self.request("PUT", "/api/items")
        self.assertEqual(405, status)
        self.assertEqual("GET, POST", headers["allow"])

    def test_html_route_with_query(self) -> None:
        status, headers, raw = self.request("GET", "/hello?name=Ada")
        self.assertEqual(200, status)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertEqual(b"<p>Hello Ada</p>", raw)

    def test_static_files_and_index(self) -> None:
        status, headers, raw = self.request("GET", "/")
        self.assertEqual((200, b"<h1>home</h1>"), (status, raw))
        status, headers, raw = self.request("GET", "/js/app.js")
        self.assertEqual(200, status)
        self.assertTrue(headers["content-type"].startswith("text/javascript"))
        self.assertEqual(404, self.request("GET", "/missing.css")[0])

    def test_static_path_traversal_blocked(self) -> None:
        for path in ("/../secret_outside_root.txt", "/js/../../secret_outside_root.txt",
                     "/%2e%2e/secret_outside_root.txt", "/..%5csecret_outside_root.txt", "/C:/Windows/win.ini"):
            status, _, raw = self.request("GET", path)
            self.assertNotEqual(b"top secret", raw, path)
            self.assertEqual(404, status, path)

    def test_mcp_endpoint_still_works(self) -> None:
        status, _, raw = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                       "params": {"name": "ping_tool", "arguments": {}}})
        self.assertEqual(200, status)
        self.assertEqual("pong", json.loads(raw)["result"]["content"][0]["text"])

    def test_sse_response_broadcast(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/events")
            resp = conn.getresponse()
            self.assertEqual("text/event-stream", resp.getheader("Content-Type"))
            data = b""
            deadline = time.time() + 3
            while b"event: ready" not in data and time.time() < deadline:
                data += resp.readline()
            self.hub.broadcast({"hello": "browser"})
            while b'"hello":"browser"' not in data and time.time() < deadline:
                data += resp.readline()
            self.assertIn(b'data: {"hello":"browser"}', data)
        finally:
            conn.close()

    def test_route_conflicting_with_endpoint_rejected(self) -> None:
        with self.assertRaises(McpConfigurationError):
            self.server.app.add_route("/mcp", lambda r: None)


if __name__ == "__main__":
    unittest.main()
