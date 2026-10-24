import enum
import json
import unittest
from dataclasses import dataclass
from typing import Annotated, Dict, List, Literal, Optional

from tests.handlers import TestHandlers

from py_http_mcp_server import CancellationToken, McpError, McpRegistry, Param, mcp_tool
from py_http_mcp_server._typing import convert_string, convert_value, json_schema


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


@dataclass
class Address:
    street: str
    zip_code: Optional[str] = None


class McpRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = McpRegistry()
        self.registry.register(TestHandlers)

    def test_tool_registered_and_invoked(self) -> None:
        self.assertEqual("15", self.registry.invoke_tool("add", {"a": 5, "b": 10}))

    def test_tool_arguments_case_insensitive_and_string_coercion(self) -> None:
        self.assertEqual("15", self.registry.invoke_tool("add", {"A": "5", "B": 10}))

    def test_async_tool_with_cancellation_token(self) -> None:
        self.assertEqual("Async: Devin", self.registry.invoke_tool("async_echo", {"text": "Devin"}, CancellationToken()))
        tools_json = json.dumps(self.registry.tools_list())
        self.assertNotIn('"ct"', tools_json)

    def test_annotations_icons_and_output_schema(self) -> None:
        result = self.registry.tools_list(ttl_ms=60000)
        self.assertEqual(60000, result["ttlMs"])
        self.assertEqual("public", result["cacheScope"])
        tools = {t["name"]: t for t in result["tools"]}

        add = tools["add"]
        self.assertEqual("Add Numbers", add["title"])
        self.assertTrue(add["annotations"]["readOnlyHint"])
        self.assertTrue(add["annotations"]["idempotentHint"])
        self.assertNotIn("destructiveHint", add["annotations"])
        self.assertEqual(["a", "b"], add["inputSchema"]["required"])
        self.assertEqual("integer", add["inputSchema"]["properties"]["a"]["type"])
        self.assertEqual(2, len(add["icons"]))
        self.assertEqual("https://example.com/icons/add.svg", add["icons"][0]["src"])
        self.assertEqual("dark", add["icons"][1]["theme"])
        self.assertEqual(["48x48"], add["icons"][1]["sizes"])

        weather = tools["get_weather"]
        self.assertEqual("object", weather["outputSchema"]["type"])
        self.assertEqual({"City", "TemperatureC", "Condition"}, set(weather["outputSchema"]["properties"]))

        echo = tools["async_echo"]
        self.assertEqual("Text to echo", echo["inputSchema"]["properties"]["text"]["description"])

    def test_resource_registered_and_read(self) -> None:
        self.assertEqual(("Hello Resource", "text/plain"), self.registry.read_resource("test://data"))

    def test_resource_template_read_dynamically(self) -> None:
        content, mime = self.registry.read_resource("users://42/profile")
        self.assertEqual('{"id":42,"name":"User_42"}', content)
        self.assertEqual("application/json", mime)
        templates = self.registry.resource_templates_list()["resourceTemplates"]
        self.assertEqual("users://{id}/profile", templates[0]["uriTemplate"])

    def test_resource_not_found(self) -> None:
        with self.assertRaises(McpError) as ctx:
            self.registry.read_resource("nope://x")
        self.assertEqual(-32602, ctx.exception.code)

    def test_prompt_registered_and_generated(self) -> None:
        self.assertEqual("Hello Alice", self.registry.get_prompt("greet", {"name": "Alice"}))
        prompt = self.registry.prompts_list()["prompts"][0]
        self.assertEqual([{"name": "name", "description": "User name", "required": True}], prompt["arguments"])

    def test_missing_required_parameter(self) -> None:
        with self.assertRaises(McpError) as ctx:
            self.registry.invoke_tool("add", {"a": 1})
        self.assertIn("'b'", ctx.exception.message)

    def test_function_decorator_registration_and_rich_schema(self) -> None:
        registry = McpRegistry()

        @registry.tool(params={"tags": "Tag list"})
        def ship(
            color: Color,
            address: Address,
            tags: List[str],
            mode: Literal["fast", "slow"] = "fast",
            notes: Optional[str] = None,
            size: Annotated[str, Param("T-shirt size", enum=["S", "M", "L"])] = "M",
        ) -> Dict[str, str]:
            """Ship a parcel."""
            return {"color": color.value, "street": address.street, "tags": ",".join(tags), "mode": mode, "size": size}

        tool = registry.tools_list()["tools"][0]
        self.assertEqual("ship", tool["name"])
        self.assertEqual("Ship a parcel.", tool["description"])
        props = tool["inputSchema"]["properties"]
        self.assertEqual(["RED", "GREEN"], props["color"]["enum"])
        self.assertEqual("object", props["address"]["type"])
        self.assertEqual(["street"], props["address"]["required"])
        self.assertEqual({"type": "string"}, props["tags"]["items"])
        self.assertEqual("Tag list", props["tags"]["description"])
        self.assertEqual(["fast", "slow"], props["mode"]["enum"])
        self.assertEqual(["S", "M", "L"], props["size"]["enum"])
        self.assertEqual("M", props["size"]["default"])
        self.assertEqual(["color", "address", "tags"], tool["inputSchema"]["required"])

        raw = registry.invoke_tool_raw("ship", {"color": "green", "address": {"Street": "Main"}, "tags": ["a", "b"]})
        self.assertEqual({"color": "green", "street": "Main", "tags": "a,b", "mode": "fast", "size": "M"}, raw)

    def test_bare_decorator_and_staticmethod(self) -> None:
        class Handlers:
            @mcp_tool
            @staticmethod
            def ping() -> str:
                return "pong"

        registry = McpRegistry()
        registry.register(Handlers)
        self.assertEqual("pong", registry.invoke_tool("ping", {}))


class TypeConversionTests(unittest.TestCase):
    def test_convert_value(self) -> None:
        self.assertEqual(3, convert_value(3.0, int))
        self.assertEqual(3, convert_value("3", int))
        self.assertTrue(convert_value("TRUE", bool))
        self.assertEqual(Color.RED, convert_value("red", Color))
        self.assertEqual(Color.GREEN, convert_value("GREEN", Color))
        self.assertEqual('{"a":1}', convert_value({"a": 1}, str))
        self.assertEqual([1, 2], convert_value(["1", 2], List[int]))
        self.assertIsNone(convert_value(None, Optional[int]))
        self.assertEqual(Address("x", "1"), convert_value({"STREET": "x", "zip_code": "1"}, Address))
        with self.assertRaises((TypeError, ValueError)):
            convert_value("abc", int)
        with self.assertRaises(ValueError):
            convert_value("medium", Literal["fast", "slow"])

    def test_convert_string(self) -> None:
        self.assertEqual(42, convert_string("42", int))
        self.assertEqual([1, 2], convert_string("[1, 2]", List[int]))

    def test_schema_optional_and_nested(self) -> None:
        self.assertEqual({"type": "integer"}, json_schema(Optional[int]))
        self.assertEqual({"type": "object", "additionalProperties": {"type": "number"}}, json_schema(Dict[str, float]))


if __name__ == "__main__":
    unittest.main()
