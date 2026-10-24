"""Shared test handlers (mirrors TestHandlers in the .NET test project)."""

import asyncio
from dataclasses import dataclass
from typing import Annotated, List

from py_http_mcp_server import CancellationToken, McpContent, Param, mcp_handler, mcp_icon, mcp_prompt, mcp_resource, mcp_resource_template, mcp_tool


@dataclass
class WeatherReport:
    City: str
    TemperatureC: float
    Condition: str


@mcp_handler
class TestHandlers:
    __test__ = False  # not a pytest test class

    @mcp_tool("add", "Adds two numbers", title="Add Numbers", read_only_hint=True, idempotent_hint=True,
              icon_src="https://example.com/icons/add.svg", icon_mime_type="image/svg+xml")
    @mcp_icon("https://example.com/icons/add-dark.png", "image/png", "dark", "48x48")
    def add(self, a: int, b: int) -> int:
        return a + b

    @mcp_tool("async_echo", "Echoes asynchronously")
    async def async_echo(self, text: Annotated[str, Param("Text to echo")], ct: CancellationToken) -> str:
        await asyncio.sleep(0.005)
        return f"Async: {text}"

    @mcp_tool("get_weather", "Gets structured weather report", use_structured_output=True, read_only_hint=True)
    async def get_weather(self, city: str) -> WeatherReport:
        return WeatherReport(city, 22.5, "Sunny")

    @mcp_tool("failing_tool", "Throws an exception to test isError = true")
    def failing_tool(self) -> str:
        raise RuntimeError("Database offline")

    @mcp_tool("rich_content_tool", "Returns multiple rich McpContent blocks")
    def rich_content_tool(self) -> List[McpContent]:
        return [
            McpContent.create_text("Summary"),
            McpContent.create_image("iVBORw0KGgo=", "image/png"),
            McpContent.create_audio("UklGRiQAAABX", "audio/wav"),
            McpContent.create_resource_link("users://42/profile", "User 42", "Profile link", "application/json"),
            McpContent.create_embedded_resource("test://data", "Embedded text", "text/plain"),
        ]

    @mcp_tool("slow_tool", "Sleeps cooperatively until cancelled")
    def slow_tool(self, ct: CancellationToken) -> str:
        if ct.wait(5):
            ct.raise_if_cancelled()
        return "finished"

    @mcp_resource("test://data", "Test Data", "A test resource", "text/plain")
    def get_test_data(self) -> str:
        return "Hello Resource"

    @mcp_resource_template("users://{id}/profile", "User Profile", "Gets user profile by ID", "application/json")
    def get_user_profile(self, id: int) -> str:  # noqa: A002 - mirrors the .NET sample
        return f'{{"id":{id},"name":"User_{id}"}}'

    @mcp_prompt("greet", "Greets the user")
    def greet(self, name: Annotated[str, Param("User name")]) -> str:
        return f"Hello {name}"
