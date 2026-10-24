"""Framework integration tests: ASGI (pure stdlib driver), FastAPI (if installed), WSGI middleware, discover()."""

import asyncio
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from typing import Any, Dict, List

from py_http_mcp_server import McpAsgiMiddleware, McpServer, McpWsgiMiddleware

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    HAS_FASTAPI = False


def make_server() -> McpServer:
    mcp = McpServer(server_name="IntegrationServer", enable_logging=False, enable_http_logging=False,
                    sse_keepalive_interval=0.1)

    @mcp.tool()
    def add(a: int, b: int) -> int:
        """Adds two numbers."""
        return a + b

    @mcp.tool()
    async def which_loop() -> str:
        """Reports the id of the event loop running this coroutine."""
        return str(id(asyncio.get_running_loop()))

    return mcp


def rpc(method: str, params: Any = None, id_: int = 1) -> bytes:
    msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode()


async def asgi_call(app, method: str, path: str, body: bytes = b"", headers=None, root_path: str = "") -> Dict[str, Any]:
    """Minimal ASGI client for non-streaming requests."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": root_path, "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")] + [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 5555), "server": ("testserver", 80),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent: List[Dict[str, Any]] = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body_out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return {"status": start["status"], "headers": {k.decode(): v.decode() for k, v in start["headers"]}, "body": body_out}


class AsgiAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mcp = make_server()

    def tearDown(self) -> None:
        self.mcp.stop()

    def test_post_tool_call_and_async_tool_runs_on_host_loop(self) -> None:
        async def scenario():
            app = self.mcp.asgi_app
            res = await asgi_call(app, "POST", "/mcp", rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}))
            self.assertEqual(200, res["status"])
            self.assertEqual("5", json.loads(res["body"])["result"]["content"][0]["text"])
            res = await asgi_call(app, "POST", "/mcp", rpc("tools/call", {"name": "which_loop", "arguments": {}}))
            loop_id = json.loads(res["body"])["result"]["content"][0]["text"]
            self.assertEqual(str(id(asyncio.get_running_loop())), loop_id)

        asyncio.run(scenario())

    def test_initialize_session_and_root_path(self) -> None:
        async def scenario():
            res = await asgi_call(self.mcp.asgi_app, "POST", "/api/mcp",
                                  rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                     "clientInfo": {"name": "t", "version": "1"}}),
                                  root_path="/api")
            self.assertEqual(200, res["status"])
            self.assertIn("mcp-session-id", res["headers"])
            self.assertEqual("2025-06-18", res["headers"]["mcp-protocol-version"])

        asyncio.run(scenario())

    def test_middleware_passes_through_non_mcp_paths(self) -> None:
        calls = []

        async def host_app(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"host"})

        app = McpAsgiMiddleware(host_app, server=self.mcp)

        async def scenario():
            res = await asgi_call(app, "GET", "/hello")
            self.assertEqual(b"host", res["body"])
            res = await asgi_call(app, "POST", "/mcp", rpc("ping"))
            self.assertEqual({"jsonrpc": "2.0", "id": 1, "result": {}}, json.loads(res["body"]))

        asyncio.run(scenario())
        self.assertEqual(["/hello"], calls)

    def test_sse_stream_and_client_disconnect_cleans_up(self) -> None:
        async def scenario():
            disconnect = asyncio.Event()
            sent: List[Dict[str, Any]] = []
            scope = {"type": "http", "method": "GET", "path": "/mcp", "root_path": "", "scheme": "http",
                     "query_string": b"", "headers": [(b"host", b"testserver")]}
            first = True

            async def receive():
                nonlocal first
                if first:
                    first = False
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)

            task = asyncio.ensure_future(self.mcp.asgi_app(scope, receive, send))
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(self.mcp.sessions) == 1 and any(m.get("body") for m in sent):
                    break
            self.assertEqual(1, len(self.mcp.sessions))
            self.mcp.notify_tools_list_changed()
            for _ in range(100):
                await asyncio.sleep(0.02)
                if b"list_changed" in b"".join(m.get("body", b"") for m in sent):
                    break
            start = sent[0]
            self.assertEqual(200, start["status"])
            self.assertIn((b"content-type", b"text/event-stream"), [tuple(h) for h in start["headers"]])
            self.assertIn(b"notifications/tools/list_changed", b"".join(m.get("body", b"") for m in sent))
            disconnect.set()
            await asyncio.wait_for(task, 3)
            for _ in range(50):
                if len(self.mcp.sessions) == 0:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(0, len(self.mcp.sessions))

        asyncio.run(scenario())

    def test_lifespan_standalone(self) -> None:
        async def scenario():
            inbox = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
            sent = []

            async def receive():
                return inbox.pop(0)

            async def send(message):
                sent.append(message["type"])

            await self.mcp.asgi_app({"type": "lifespan"}, receive, send)
            self.assertEqual(["lifespan.startup.complete", "lifespan.shutdown.complete"], sent)

        asyncio.run(scenario())


@unittest.skipUnless(HAS_FASTAPI, "fastapi not installed")
class FastApiIntegrationTests(unittest.TestCase):
    def test_mount_on_fastapi_app(self) -> None:
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/health")
        def health() -> Dict[str, str]:
            return {"status": "ok"}

        mcp = make_server()
        mcp.mount(app)
        try:
            with TestClient(app) as client:
                self.assertEqual({"status": "ok"}, client.get("/health").json())
                res = client.post("/mcp", content=rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                                     "clientInfo": {"name": "t", "version": "1"}}),
                                  headers={"Content-Type": "application/json"})
                self.assertEqual(200, res.status_code)
                session = res.headers["mcp-session-id"]
                res = client.post("/mcp", content=rpc("tools/call", {"name": "add", "arguments": {"a": 40, "b": 2}}),
                                  headers={"Content-Type": "application/json", "Mcp-Session-Id": session})
                self.assertEqual("42", res.json()["result"]["content"][0]["text"])
                res = client.post("/mcp", content=rpc("tools/call", {"name": "which_loop", "arguments": {}}),
                                  headers={"Content-Type": "application/json"})
                self.assertFalse(res.json()["result"]["isError"])
                self.assertEqual(200, client.delete("/mcp", headers={"Mcp-Session-Id": session}).status_code)
                self.assertNotIn("/mcp", client.get("/openapi.json").json()["paths"])
        finally:
            mcp.stop()

    def test_add_middleware_variant(self) -> None:
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/")
        def root() -> Dict[str, str]:
            return {"app": "fastapi"}

        mcp = make_server()
        app.add_middleware(McpAsgiMiddleware, server=mcp)
        try:
            with TestClient(app) as client:
                self.assertEqual({"app": "fastapi"}, client.get("/").json())
                res = client.post("/mcp", content=rpc("tools/list"), headers={"Content-Type": "application/json"})
                self.assertEqual({"add", "which_loop"}, {t["name"] for t in res.json()["result"]["tools"]})
        finally:
            mcp.stop()


class WsgiMiddlewareTests(unittest.TestCase):
    def test_wsgi_middleware_routes_mcp_and_passes_through(self) -> None:
        def host_app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"host:" + environ["PATH_INFO"].encode()]

        mcp = make_server()
        app = McpWsgiMiddleware(host_app, server=mcp)

        def call(path, body=b""):
            environ = {"REQUEST_METHOD": "POST" if body else "GET", "PATH_INFO": path, "SCRIPT_NAME": "",
                       "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body)),
                       "CONTENT_TYPE": "application/json", "wsgi.url_scheme": "http", "HTTP_HOST": "x"}
            status = {}
            out = b"".join(app(environ, lambda s, h: status.update(s=s)))
            return status["s"], out

        self.assertEqual(("200 OK", b"host:/about"), call("/about"))
        status, out = call("/mcp", rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}))
        self.assertEqual("200 OK", status)
        self.assertEqual("2", json.loads(out)["result"]["content"][0]["text"])
        mcp.stop()

    def test_mount_on_flask_like_app(self) -> None:
        class FakeFlask:
            def __init__(self):
                self.wsgi_app = lambda environ, start_response: [b"flask"]

            def add_url_rule(self, *args, **kwargs):  # presence marks it as Flask
                pass

        mcp = make_server()
        app = mcp.mount(FakeFlask())
        self.assertIsInstance(app.wsgi_app, McpWsgiMiddleware)


class DiscoverTests(unittest.TestCase):
    def test_discover_package_recursively(self) -> None:
        root = tempfile.mkdtemp()
        pkg = os.path.join(root, "disc_pkg_demo")
        os.makedirs(os.path.join(pkg, "sub"))
        open(os.path.join(pkg, "__init__.py"), "w").close()
        open(os.path.join(pkg, "sub", "__init__.py"), "w").close()
        with open(os.path.join(pkg, "tools.py"), "w") as fh:
            fh.write(textwrap.dedent('''
                from py_http_mcp_server import mcp_tool, mcp_handler, mcp_prompt

                @mcp_tool
                def shout(text: str) -> str:
                    """Upper-cases text."""
                    return text.upper()

                @mcp_handler
                class Greeter:
                    @mcp_prompt("hello_prompt", "Says hello")
                    def hello(self, name: str) -> str:
                        return f"Hello {name}"
            '''))
        with open(os.path.join(pkg, "sub", "more.py"), "w") as fh:
            fh.write("from py_http_mcp_server import mcp_tool\n\n@mcp_tool('nested_tool', 'Nested')\ndef nested() -> str:\n    return 'deep'\n")
        sys.path.insert(0, root)
        try:
            mcp = McpServer(enable_logging=False).discover("disc_pkg_demo")
            self.assertEqual({"shout", "nested_tool"}, set(mcp.registry.tools))
            self.assertEqual({"hello_prompt"}, set(mcp.registry.prompts))
            self.assertEqual("HI", mcp.registry.invoke_tool("shout", {"text": "hi"}))
        finally:
            sys.path.remove(root)


if __name__ == "__main__":
    unittest.main()
