"""Sample server that is BOTH a web application and an MCP server on the same port.

Run:      python examples/sample_server.py
Web UI:   http://127.0.0.1:5000/          (notes board, MCP playground, live activity)
MCP:      http://127.0.0.1:5000/mcp       (MCP Inspector / Antigravity / Claude / Cursor)
"""

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Annotated, List

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import notes_app  # noqa: E402  (shared notes board: REST API + MCP tools)
from py_http_mcp_server import (  # noqa: E402
    CancellationToken,
    McpContent,
    McpIcon,
    McpServer,
    Param,
    mcp_handler,
    mcp_icon,
    mcp_prompt,
    mcp_resource,
    mcp_resource_template,
    mcp_tool,
)

server = McpServer(
    server_name="SampleMcpServer",
    server_version="1.0.0",
    server_title="Sample Python MCP Server",
    website_url="https://example.com",
    instructions="Use these tools to do arithmetic, check the weather and read user profiles.",
    use_default_cors=True,
    list_cache_ttl_ms=60000,
    icons=[McpIcon(src="/icon.png", mime_type="image/png", sizes=["256x256"], theme="light")],
)


# ---------------------------------------------------------------------------------------------
# Style 1: decorate plain functions directly on the server
# ---------------------------------------------------------------------------------------------
@server.tool("calculate_sum", "Calculates the sum of two numbers.", title="Calculate Sum",
             read_only_hint=True, idempotent_hint=True)
def calculate_sum(a: Annotated[int, Param("First number")], b: Annotated[int, Param("Second number")]) -> int:
    return a + b


@server.tool()
def echo_message(message: str) -> str:
    """Echoes a message back to the user."""
    return f"Echo: {message}"


@server.resource("sample://greeting", "Greeting Resource", "A simple text greeting resource")
def greeting() -> str:
    return "Hello from the Python MCP Streamable HTTP Server!"


# ---------------------------------------------------------------------------------------------
# Style 2: handler classes (equivalent of [McpHandler] classes in .NET)
# ---------------------------------------------------------------------------------------------
@dataclass
class WeatherReport:
    city: str
    temperature_c: float
    condition: str


@mcp_handler
class MyAiTools:
    @mcp_tool("get_weather", "Returns structured weather data.", use_structured_output=True, read_only_hint=True)
    @mcp_icon("/icons/weather-light.png", "image/png", "light", "48x48")
    @mcp_icon("/icons/weather-dark.png", "image/png", "dark", "48x48")
    async def get_weather(
        self,
        city: Annotated[str, Param("City name", enum=["Seattle", "London", "Tokyo"])],
        ct: CancellationToken,
    ) -> WeatherReport:
        await asyncio.sleep(0.01)
        return WeatherReport(city, 24.5, "Sunny")

    @mcp_tool("generate_chart", "Returns a chart image and a resource link.")
    def generate_chart(self, metric: str) -> List[McpContent]:
        return [
            McpContent.create_text(f"Generated chart for {metric}"),
            McpContent.create_image("iVBORw0KGgoAAAANSUhEUg==", "image/png"),
            McpContent.create_resource_link("system://status", "System Status", "Live status JSON", "application/json"),
        ]

    @mcp_tool("long_job", "Simulates a long-running job (try calling it with params.task to run it as a task).")
    def long_job(self, seconds: float, ct: CancellationToken) -> str:
        if ct.wait(seconds):  # cancellable sleep
            ct.raise_if_cancelled()
        return f"Job finished after {seconds}s"

    @mcp_resource("system://status", "System Status", "Returns the current system status.", "application/json")
    def get_status(self) -> dict:
        return {"status": "online", "uptime": "99.9%"}

    @mcp_resource_template("users://{id}/profile", "User Profile", "Fetches a user profile by ID.", "application/json")
    def get_user_profile(self, id: int) -> dict:  # noqa: A002
        return {"id": id, "role": "developer"}

    @mcp_prompt("greet_user", "Generates a greeting prompt.", title="Greet User")
    def greet(self, username: Annotated[str, Param("User's display name")]) -> str:
        return f"Hello {username}, how can I help you today?"


server.register(MyAiTools)

# ---------------------------------------------------------------------------------------------
# Web application on the same server: REST API + live events + static single-page UI
# ---------------------------------------------------------------------------------------------
notes_app.install(server)
server.static("/", os.path.join(_HERE, "web"))

if __name__ == "__main__":
    server.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")))
