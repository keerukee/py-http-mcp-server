import io
import json
import unittest
from wsgiref.util import setup_testing_defaults

from tests.handlers import TestHandlers

from py_http_mcp_server import McpServer


class WsgiAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = McpServer(enable_logging=False, enable_http_logging=False)
        self.server.register(TestHandlers)
        self.app = self.server.wsgi_app

    def _call(self, method: str, body: bytes = b"", headers=None):
        environ = {"REQUEST_METHOD": method, "PATH_INFO": "/mcp", "wsgi.input": io.BytesIO(body),
                   "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/json"}
        environ.update(headers or {})
        setup_testing_defaults(environ)
        captured = {}

        def start_response(status, response_headers):
            captured["status"], captured["headers"] = status, dict(response_headers)

        result = self.app(environ, start_response)
        return captured["status"], captured["headers"], result

    def test_post_tools_call(self) -> None:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "add", "arguments": {"a": 1, "b": 2}}}).encode()
        status, headers, chunks = self._call("POST", body)
        self.assertEqual("200 OK", status)
        self.assertNotIn("Connection", headers)
        self.assertEqual("3", json.loads(b"".join(chunks))["result"]["content"][0]["text"])

    def test_get_stream_is_iterable_and_closable(self) -> None:
        status, headers, stream = self._call("GET", headers={"HTTP_MCP_SESSION_ID": "abc"})
        self.assertEqual("200 OK", status)
        self.assertEqual("abc", headers["Mcp-Session-Id"])
        self.assertTrue(next(iter(stream)).startswith(b"id: 1\nevent: endpoint"))
        self.assertEqual(1, len(self.server.sessions))
        stream.close()
        self.assertEqual(0, len(self.server.sessions))


if __name__ == "__main__":
    unittest.main()
