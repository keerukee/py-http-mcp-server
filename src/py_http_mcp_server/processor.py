"""JSON-RPC request processing for MCP (spec revisions 2024-11-05 .. 2026-07-28).

The processor is transport-agnostic: give it a JSON string (or decoded payload) and it returns
the JSON-RPC response (or ``None`` for notifications / client responses -> HTTP 202).
"""

import base64
import concurrent.futures
import contextvars
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import _json
from .cancellation import CancellationToken
from .content import McpContent, McpToolResult, resolve_icons
from .context import current_base_url, get_current_request
from .exceptions import McpCancelledError, McpError
from .options import McpServerOptions
from .registry import McpRegistry, _to_text
from .tasks import McpTaskStore

__all__ = ["McpRequestProcessor", "NO_INITIALIZE_VERSION", "has_initialize_handshake", "negotiate_initialize_version"]

logger = logging.getLogger("py_http_mcp_server")

#: First protocol version that replaced the ``initialize`` handshake with per-request ``_meta`` (SEP-2567).
NO_INITIALIZE_VERSION = "2026-07-28"

_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
_META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
_TASKS_EXTENSION = "io.modelcontextprotocol/tasks"

MethodHandler = Callable[[Dict[str, Any]], Any]


def has_initialize_handshake(version: str) -> bool:
    """Versions before 2026-07-28 use the ``initialize`` handshake."""
    return version < NO_INITIALIZE_VERSION


def negotiate_initialize_version(client_version: Optional[str], options: McpServerOptions) -> str:
    """Pick the protocol version for ``initialize`` (capped at the last handshake-based revision)."""
    if client_version and has_initialize_handshake(client_version) and options.is_supported_version(client_version):
        return client_version
    if has_initialize_handshake(options.protocol_version) and options.is_supported_version(options.protocol_version):
        return options.protocol_version
    candidates = sorted((v for v in options.supported_protocol_versions if has_initialize_handshake(v)), reverse=True)
    return candidates[0] if candidates else "2025-11-25"


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


class McpRequestProcessor:
    """Processes MCP JSON-RPC messages using an :class:`McpRegistry`."""

    def __init__(self, registry: McpRegistry, options: McpServerOptions, task_store: Optional[McpTaskStore] = None) -> None:
        self.registry = registry
        self.options = options
        self.task_store = task_store if task_store is not None else McpTaskStore()
        self._custom_methods: Dict[str, MethodHandler] = {}
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()
        self._in_flight: Dict[Tuple[Optional[str], str], CancellationToken] = {}
        self._in_flight_lock = threading.Lock()

    # ---- extension points --------------------------------------------------------------------
    def register_method(self, method: str, handler: MethodHandler) -> None:
        """Register (or override) a JSON-RPC method handler: ``handler(params: dict) -> result``."""
        self._custom_methods[method] = handler

    def _log(self, message: str, *args: Any) -> None:
        if self.options.enable_logging:
            logger.info(message, *args)

    # ---- header validation -------------------------------------------------------------------
    def validate_routing_headers(self, payload: Any, method_header: Optional[str], name_header: Optional[str]) -> Optional[str]:
        """Validate ``Mcp-Method`` / ``Mcp-Name`` headers against the body (SEP-2243).

        Returns ``None`` when valid (or headers absent), otherwise an error message.
        """
        if not method_header and not name_header:
            return None
        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = _json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                return None  # let the main processor report the parse error
        if not isinstance(payload, dict):
            return None
        if method_header:
            body_method = _str_or_none(payload.get("method"))
            if body_method != method_header:
                return f"Header 'Mcp-Method' ({method_header}) does not match JSON-RPC method ({body_method or 'null'})."
        if name_header:
            body_name = None
            params = payload.get("params")
            if isinstance(params, dict):
                for key in ("name", "uri", "taskId"):
                    if key in params:
                        body_name = _str_or_none(params[key])
                        break
            if body_name != name_header:
                return f"Header 'Mcp-Name' ({name_header}) does not match JSON-RPC target name/uri ({body_name or 'null'})."
        return None

    # ---- processing --------------------------------------------------------------------------
    def process(self, json_text: Any) -> Optional[str]:
        """Process a single or batch JSON-RPC payload (``str``/``bytes``); returns JSON text or ``None``."""
        try:
            payload = _json.loads(json_text)
        except (ValueError, UnicodeDecodeError) as exc:
            self._log("[MCP Parse Error] %s", exc)
            return _json.dumps(self.parse_error_response(exc))
        response = self.process_payload(payload)
        return None if response is None else _json.dumps(response)

    @staticmethod
    def parse_error_response(exc: Exception) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": McpError.PARSE_ERROR, "message": f"Parse error: {exc}"}}

    def process_payload(self, payload: Any) -> Any:
        """Process an already-decoded JSON-RPC payload; returns a JSON-able response or ``None``."""
        if isinstance(payload, list):
            if not payload:
                return self._error_response(None, McpError.invalid_request("Invalid Request"))
            responses = [r for r in (self._process_single(m) for m in payload) if r is not None]
            return responses or None
        if isinstance(payload, dict):
            return self._process_single(payload)
        return self._error_response(None, McpError.invalid_request("Invalid Request"))

    @staticmethod
    def _error_response(request_id: Any, error: McpError) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": error.to_error_object()}

    def _process_single(self, message: Any) -> Optional[Dict[str, Any]]:
        request_id: Any = None
        try:
            if not isinstance(message, dict):
                raise McpError.invalid_request("Invalid Request")
            has_id = "id" in message
            raw_id = message.get("id")
            if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool):
                request_id = raw_id

            # A JSON-RPC response from the client (e.g. sampling/elicitation result) -> 202 Accepted
            if "method" not in message:
                return None
            method = message["method"]
            if not isinstance(method, str):
                raise McpError.invalid_request("Invalid Request: 'method' must be a string")
            params = message.get("params")
            params = params if isinstance(params, dict) else {}

            if not has_id or method.startswith("notifications/"):
                self._log("[MCP] Notification: %s", method)
                self._handle_notification(method, params)
                return None

            self._validate_request_meta(params, method)
            result = self._dispatch(method, params, request_id)
            return {"jsonrpc": "2.0", "id": request_id, "result": _json.to_jsonable(result)}
        except McpError as exc:
            self._log("[MCP Error] %s: %s", exc.code, exc.message)
            return self._error_response(request_id, exc)
        except Exception as exc:  # noqa: BLE001 - JSON-RPC internal error boundary
            if self.options.enable_logging:
                logger.exception("[MCP Error] %s", exc)
            return self._error_response(request_id, McpError.internal_error(str(exc) or type(exc).__name__))

    def _dispatch(self, method: str, params: Dict[str, Any], request_id: Any) -> Any:
        custom = self._custom_methods.get(method)
        if custom is not None:
            return custom(params)

        opts = self.options
        if method == "initialize":
            return self._handle_initialize(params)
        if method == "server/discover":
            return self._handle_server_discover()
        if method == "ping":
            return {}
        if method == "tools/list":
            return self.registry.tools_list(current_base_url(), opts.list_cache_ttl_ms, opts.cache_scope)
        if method == "tools/call":
            return self._handle_tool_call(params, request_id)
        if method == "resources/list":
            return self.registry.resources_list(current_base_url(), opts.list_cache_ttl_ms, opts.cache_scope)
        if method == "resources/read":
            return self._handle_resource_read(params)
        if method == "resources/templates/list":
            return self.registry.resource_templates_list(current_base_url(), opts.list_cache_ttl_ms, opts.cache_scope)
        if method == "prompts/list":
            return self.registry.prompts_list(current_base_url(), opts.list_cache_ttl_ms, opts.cache_scope)
        if method == "prompts/get":
            return self._handle_prompt_get(params)
        if opts.enable_tasks_extension:
            if method == "tasks/get":
                return self._handle_tasks_get(params)
            if method == "tasks/list":
                return {"tasks": [t.to_dict() for t in self.task_store.tasks]}
            if method == "tasks/update":
                return self._handle_tasks_update(params)
            if method == "tasks/cancel":
                return self._handle_tasks_cancel(params)
        if method == "roots/list":
            return {"roots": []}
        if method == "logging/setLevel":
            return {}
        if method == "completion/complete":
            return {"completion": {"values": [], "total": 0, "hasMore": False}}
        raise McpError.method_not_found(method)

    def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        if method == "notifications/cancelled":
            request_id = params.get("requestId")
            if request_id is not None:
                with self._in_flight_lock:
                    token = self._in_flight.get((self._session_key(), str(request_id)))
                if token is not None:
                    token.cancel()

    @staticmethod
    def _session_key() -> Optional[str]:
        request = get_current_request()
        return request.session_id if request is not None else None

    def _validate_request_meta(self, params: Dict[str, Any], method: str) -> None:
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            return
        meta_version = _str_or_none(meta.get(_META_PROTOCOL_VERSION))
        if (
            meta_version
            and method not in ("initialize", "server/discover")
            and self.options.validate_protocol_version_header
            and not self.options.is_supported_version(meta_version)
        ):
            raise McpError.invalid_request(
                f"Unsupported protocolVersion '{meta_version}' in _meta. "
                f"Supported versions: {', '.join(self.options.supported_protocol_versions)}"
            )
        client_info = meta.get(_META_CLIENT_INFO)
        if isinstance(client_info, dict):
            self._log("[MCP _meta] Client: %s %s", client_info.get("name", "Unknown"), client_info.get("version", "Unknown"))

    # ---- lifecycle ---------------------------------------------------------------------------
    def build_server_info(self) -> Dict[str, Any]:
        opts = self.options
        info: Dict[str, Any] = {"name": opts.server_name, "version": opts.server_version}
        if opts.server_title:
            info["title"] = opts.server_title
        if opts.website_url:
            info["websiteUrl"] = opts.website_url
        if opts.icons:
            info["icons"] = resolve_icons(opts.icons, current_base_url())
        return info

    def build_server_capabilities(self, include_extensions: bool) -> Dict[str, Any]:
        capabilities: Dict[str, Any] = {
            "tools": {"listChanged": True},
            "resources": {"subscribe": False, "listChanged": True},
            "prompts": {"listChanged": True},
            "logging": {},
        }
        if include_extensions and self.options.enable_tasks_extension:
            capabilities["extensions"] = {_TASKS_EXTENSION: {}}
        return capabilities

    def _handle_server_discover(self) -> Dict[str, Any]:
        opts = self.options
        response: Dict[str, Any] = {
            "resultType": "complete",
            "supportedVersions": list(opts.supported_protocol_versions),
            "capabilities": self.build_server_capabilities(include_extensions=True),
            "ttlMs": opts.list_cache_ttl_ms if opts.list_cache_ttl_ms is not None else 60000,
            "cacheScope": opts.cache_scope,
            "_meta": {_META_SERVER_INFO: self.build_server_info()},
        }
        if opts.instructions:
            response["instructions"] = opts.instructions
        return response

    def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested: Optional[str] = None
        if "protocolVersion" in params:
            requested = _str_or_none(params.get("protocolVersion"))
        elif isinstance(params.get("_meta"), dict):
            requested = _str_or_none(params["_meta"].get(_META_PROTOCOL_VERSION))
        negotiated = negotiate_initialize_version(requested, self.options)

        client_info = params.get("clientInfo")
        if isinstance(client_info, dict):
            self._log(
                "[MCP] Client connected: %s %s (protocol: %s)",
                client_info.get("name", "Unknown"), client_info.get("version", "Unknown"), negotiated,
            )
        response: Dict[str, Any] = {
            "protocolVersion": negotiated,
            "capabilities": self.build_server_capabilities(include_extensions=False),
            "serverInfo": self.build_server_info(),
        }
        if self.options.instructions:
            response["instructions"] = self.options.instructions
        return response

    # ---- tools -------------------------------------------------------------------------------
    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, self.options.task_max_workers), thread_name_prefix="mcp-task"
                )
            return self._executor

    def shutdown(self) -> None:
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)

    @staticmethod
    def _async_task_request(params: Dict[str, Any]) -> Tuple[bool, Optional[int]]:
        task = params.get("task")
        if isinstance(task, dict):
            return True, _int_or_none(task.get("ttl")) if "ttl" in task else _int_or_none(task.get("ttlMs"))
        meta = params.get("_meta")
        if isinstance(meta, dict) and _TASKS_EXTENSION in meta:
            meta_task = meta[_TASKS_EXTENSION]
            if isinstance(meta_task, dict):
                ttl = _int_or_none(meta_task.get("ttl")) if "ttl" in meta_task else _int_or_none(meta_task.get("ttlMs"))
                return True, ttl
            return meta_task is True, None
        return False, None

    def _handle_tool_call(self, params: Dict[str, Any], request_id: Any) -> Any:
        name = params.get("name")
        if not isinstance(name, str):
            raise McpError.invalid_params("Tool call requires a 'name' parameter.")
        arguments = params.get("arguments")
        self._log("[MCP] Tool call: %s", name)
        if name not in self.registry.tools:
            raise McpError.invalid_params(f"Unknown tool: {name}")

        if self.options.enable_tasks_extension:
            requested, ttl = self._async_task_request(params)
            if requested:
                return self._start_background_tool(name, arguments, ttl)

        token = CancellationToken()
        key = (self._session_key(), str(request_id))
        with self._in_flight_lock:
            self._in_flight[key] = token
        try:
            return self.execute_tool_formatted(name, arguments, token)
        except McpCancelledError:
            return {"content": [{"type": "text", "text": "Error: Tool execution cancelled."}], "isError": True}
        finally:
            with self._in_flight_lock:
                if self._in_flight.get(key) is token:
                    del self._in_flight[key]

    def _start_background_tool(self, name: str, arguments: Any, ttl: Optional[int]) -> Dict[str, Any]:
        store = self.task_store
        task = store.create_task(ttl=ttl if ttl is not None else 60000, poll_interval=1000,
                                 status_message=f"Executing tool '{name}'")
        snapshot = task.to_dict()

        def _run() -> None:
            token = task.cancellation_token
            try:
                result = self.execute_tool_formatted(name, arguments, token)
                if token.is_cancelled:
                    store.cancel_task(task.task_id, "Tool execution cancelled.")
                else:
                    store.complete_task(task.task_id, result)
            except McpCancelledError:
                store.cancel_task(task.task_id, "Tool execution cancelled.")
            except Exception as exc:  # noqa: BLE001
                store.fail_task(task.task_id, str(exc) or type(exc).__name__)

        ctx = contextvars.copy_context()
        self._get_executor().submit(ctx.run, _run)

        response: Dict[str, Any] = {"resultType": "task"}
        for key in ("taskId", "status", "statusMessage", "createdAt", "lastUpdatedAt", "ttl", "ttlMs", "pollInterval", "pollIntervalMs"):
            if key in snapshot:
                response[key] = snapshot[key]
        response["task"] = snapshot
        return response

    def execute_tool_formatted(self, name: str, arguments: Any, token: Optional[CancellationToken] = None) -> Dict[str, Any]:
        """Invoke a tool and format the result as an MCP ``CallToolResult``.

        Exceptions raised by the tool become tool execution errors (``isError: true``) so that
        LLM agents can self-correct (MCP 2025-11-25+). Cancellation propagates as ``McpCancelledError``.
        """
        try:
            raw = self.registry.invoke_tool_raw(name, arguments, token)
        except McpCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, McpError):
                message = exc.message
            elif isinstance(exc, KeyError) and exc.args:
                message = str(exc.args[0])  # str(KeyError) adds quotes
            else:
                message = str(exc) or type(exc).__name__
            self._log("[MCP] Tool '%s' failed: %s", name, message)
            return {"content": [{"type": "text", "text": f"Error: {message}"}], "isError": True}
        if token is not None and token.is_cancelled:
            raise McpCancelledError()
        return self.format_tool_result(raw)

    @staticmethod
    def format_tool_result(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, McpToolResult):
            return raw.to_dict()
        if isinstance(raw, McpContent):
            return {"content": [raw.to_dict()], "isError": False}
        if isinstance(raw, (list, tuple)) and raw and all(isinstance(c, McpContent) for c in raw):
            return {"content": [c.to_dict() for c in raw], "isError": False}
        if _json.is_structured_value(raw):
            return {
                "content": [{"type": "text", "text": _json.dumps(raw)}],
                "structuredContent": _json.to_jsonable(raw),
                "isError": False,
            }
        return {"content": [{"type": "text", "text": _to_text(raw)}], "isError": False}

    # ---- tasks -------------------------------------------------------------------------------
    @staticmethod
    def _require_task_id(params: Dict[str, Any], method: str) -> str:
        task_id = params.get("taskId")
        if not isinstance(task_id, str):
            raise McpError.invalid_params(f"{method} requires a 'taskId' parameter.")
        return task_id

    def _handle_tasks_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        task_id = self._require_task_id(params, "tasks/get")
        task = self.task_store.get_task(task_id)
        if task is None:
            raise McpError.invalid_params(f"Unknown taskId: {task_id}")
        return task.to_dict()

    def _handle_tasks_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        task_id = self._require_task_id(params, "tasks/update")
        task = self.task_store.update_task(
            task_id, _str_or_none(params.get("status")), _str_or_none(params.get("statusMessage")), params.get("result")
        )
        if task is None:
            raise McpError.invalid_params(f"Unknown taskId: {task_id}")
        return task.to_dict()

    def _handle_tasks_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        task_id = self._require_task_id(params, "tasks/cancel")
        task = self.task_store.cancel_task(task_id)
        if task is None:
            raise McpError.invalid_params(f"Unknown taskId: {task_id}")
        return task.to_dict()

    # ---- resources & prompts -----------------------------------------------------------------
    def _handle_resource_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise McpError.invalid_params("Resource read requires a 'uri' parameter.")
        self._log("[MCP] Resource read: %s", uri)
        content, mime_type = self.registry.read_resource(uri, CancellationToken())
        entry: Dict[str, Any] = {"uri": uri, "mimeType": mime_type}
        if isinstance(content, bytes):
            entry["blob"] = base64.b64encode(content).decode("ascii")
        else:
            entry["text"] = content
        return {"contents": [entry]}

    def _handle_prompt_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise McpError.invalid_params("Prompt get requires a 'name' parameter.")
        self._log("[MCP] Prompt get: %s", name)
        raw = self.registry.get_prompt_raw(name, params.get("arguments"), CancellationToken())
        prompt = self.registry.prompts[name]
        return {"description": prompt.description or name, "messages": self._prompt_messages(raw)}

    @staticmethod
    def _prompt_messages(raw: Any) -> List[Dict[str, Any]]:
        def one(item: Any) -> Dict[str, Any]:
            if isinstance(item, dict) and "role" in item:
                return _json.to_jsonable(item)
            if isinstance(item, McpContent):
                return {"role": "user", "content": item.to_dict()}
            return {"role": "user", "content": {"type": "text", "text": _to_text(item)}}

        if isinstance(raw, (list, tuple)):
            return [one(item) for item in raw]
        return [one(raw)]
