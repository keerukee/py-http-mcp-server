"""Registry that discovers and manages MCP tools, resources, resource templates and prompts."""

import dataclasses
import inspect
import logging
import re
import types
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Tuple, Union

from . import _json
from ._async import AsyncRunner, default_runner
from ._typing import EMPTY, convert_string, convert_value, json_schema, type_hints, unwrap_annotated, unwrap_awaitable, unwrap_optional
from .cancellation import CancellationToken
from .content import McpContent, McpIcon, McpToolResult, resolve_icons
from .decorators import (
    Param,
    PromptSpec,
    ResourceSpec,
    ResourceTemplateSpec,
    ToolSpec,
    get_icons,
    get_specs,
    is_mcp_handler,
    mcp_prompt,
    mcp_resource,
    mcp_resource_template,
    mcp_tool,
)
from .exceptions import McpConfigurationError, McpError

__all__ = [
    "McpRegistry",
    "ParameterInfo",
    "ToolRegistration",
    "ResourceRegistration",
    "ResourceTemplateRegistration",
    "PromptRegistration",
]

logger = logging.getLogger("py_http_mcp_server")

_TEMPLATE_VAR = re.compile(r"\{(\w+)\}")


# ---------------------------------------------------------------------------------------------
# Registration records
# ---------------------------------------------------------------------------------------------
@dataclass
class ParameterInfo:
    name: str
    annotation: Any
    description: str
    required: bool
    default: Any = None
    is_cancellation_token: bool = False
    enum_values: Optional[List[Any]] = None
    kind: Any = inspect.Parameter.POSITIONAL_OR_KEYWORD

    def schema(self) -> Dict[str, Any]:
        schema = dict(json_schema(self.annotation))
        schema["description"] = self.description
        if self.enum_values:
            schema["enum"] = list(self.enum_values)
        if self.default is not EMPTY and self.default is not None:
            default = self.default.name if hasattr(self.default, "name") and hasattr(self.default, "value") else self.default
            if isinstance(default, (str, int, float, bool, list, dict)):
                schema["default"] = _json.to_jsonable(default)
        return schema


@dataclass
class _Registration:
    func: Callable[..., Any]
    icon_src: Optional[str] = None
    icon_mime_type: Optional[str] = None

    @property
    def icons(self) -> List[McpIcon]:
        icons: List[McpIcon] = []
        if self.icon_src:
            icons.append(McpIcon(src=self.icon_src, mime_type=self.icon_mime_type))
        icons.extend(get_icons(self.func))
        return icons


@dataclass
class ToolRegistration(_Registration):
    name: str = ""
    description: str = ""
    title: Optional[str] = None
    parameters: List[ParameterInfo] = field(default_factory=list)
    annotations: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    use_structured_output: bool = False
    meta: Optional[Dict[str, Any]] = None


@dataclass
class ResourceRegistration(_Registration):
    uri: str = ""
    name: str = ""
    description: str = ""
    mime_type: str = "text/plain"
    title: Optional[str] = None
    parameters: List[ParameterInfo] = field(default_factory=list)


@dataclass
class ResourceTemplateRegistration(_Registration):
    uri_template: str = ""
    name: str = ""
    description: str = ""
    mime_type: str = "text/plain"
    title: Optional[str] = None
    parameters: List[ParameterInfo] = field(default_factory=list)
    pattern: Optional[Pattern[str]] = None


@dataclass
class PromptRegistration(_Registration):
    name: str = ""
    description: str = ""
    title: Optional[str] = None
    arguments: List[ParameterInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def _doc(func: Callable[..., Any]) -> str:
    return inspect.cleandoc(inspect.getdoc(func) or "")


def _is_cancellation_annotation(annotation: Any) -> bool:
    base, _ = unwrap_annotated(annotation)
    base, _ = unwrap_optional(base)
    return base is CancellationToken


def _build_parameters(func: Callable[..., Any], overrides: Dict[str, Param], default_prefix: str) -> List[ParameterInfo]:
    signature = inspect.signature(func)
    hints = type_hints(getattr(func, "__func__", func))
    result: List[ParameterInfo] = []
    for p in signature.parameters.values():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(p.name, p.annotation)
        is_token = _is_cancellation_annotation(annotation)
        base, metadata = unwrap_annotated(annotation)
        param_meta = next((m for m in metadata if isinstance(m, Param)), None) or overrides.get(p.name)
        description = (param_meta.description if param_meta and param_meta.description else None) or f"{default_prefix}: {p.name}"
        is_optional = unwrap_optional(base)[1]
        result.append(
            ParameterInfo(
                name=p.name,
                annotation=annotation,
                description=description,
                required=not is_token and p.default is EMPTY and not is_optional,
                default=p.default,
                is_cancellation_token=is_token,
                enum_values=list(param_meta.enum) if param_meta and param_meta.enum else None,
                kind=p.kind,
            )
        )
    return result


def _lookup_ci(arguments: Dict[str, Any], name: str) -> Tuple[bool, Any]:
    if name in arguments:
        return True, arguments[name]
    lowered = name.lower()
    for key, value in arguments.items():
        if isinstance(key, str) and key.lower() == lowered:
            return True, value
    return False, None


def _call(func: Callable[..., Any], parameters: List[ParameterInfo], values: Dict[str, Any]) -> Any:
    args: List[Any] = []
    kwargs: Dict[str, Any] = {}
    for p in parameters:
        if p.name not in values:
            continue
        if p.kind == inspect.Parameter.POSITIONAL_ONLY:
            args.append(values[p.name])
        else:
            kwargs[p.name] = values[p.name]
    return func(*args, **kwargs)


def _build_template_regex(uri_template: str) -> Pattern[str]:
    # "users://{id}/profile" -> ^users://(?P<id>[^/]+)/profile$
    parts: List[str] = []
    last = 0
    for match in _TEMPLATE_VAR.finditer(uri_template):
        parts.append(re.escape(uri_template[last:match.start()]))
        parts.append(f"(?P<{match.group(1)}>[^/]+)")
        last = match.end()
    parts.append(re.escape(uri_template[last:]))
    return re.compile("^" + "".join(parts) + "$", re.IGNORECASE)


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if _json.is_structured_value(value) or isinstance(value, (list, tuple, set, frozenset)):
        return _json.dumps(value)
    return str(value)


_CONTENT_TYPES = (McpContent, McpToolResult)


def _output_schema_for(func: Callable[..., Any]) -> Optional[Dict[str, Any]]:
    hints = type_hints(getattr(func, "__func__", func))
    ret = hints.get("return", EMPTY)
    if ret is EMPTY or ret is None:
        return None
    ret = unwrap_awaitable(unwrap_annotated(ret)[0])
    ret = unwrap_optional(unwrap_annotated(ret)[0])[0]
    if inspect.isclass(ret) and issubclass(ret, _CONTENT_TYPES):
        return None
    schema = json_schema(ret)
    return schema if schema.get("type") == "object" else None


# ---------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------
class McpRegistry:
    """Holds registered MCP primitives and invokes them."""

    def __init__(self, runner: Optional[AsyncRunner] = None) -> None:
        self.tools: Dict[str, ToolRegistration] = {}
        self.resources: Dict[str, ResourceRegistration] = {}
        self.resource_templates: Dict[str, ResourceTemplateRegistration] = {}
        self.prompts: Dict[str, PromptRegistration] = {}
        self.runner = runner or default_runner

    # ---- decorator-style registration --------------------------------------------------------
    def tool(self, name: Any = None, description: Optional[str] = None, **kwargs: Any) -> Any:
        """Decorator registering a function as a tool (accepts the same arguments as :func:`mcp_tool`)."""
        if callable(name):
            return self.register_function(mcp_tool(name), only_last=True)
        return lambda fn: self.register_function(mcp_tool(name, description, **kwargs)(fn), only_last=True)

    def resource(self, uri: str, name: Optional[str] = None, description: Optional[str] = None,
                 mime_type: str = "text/plain", **kwargs: Any) -> Callable[[Any], Any]:
        """Decorator registering a function as a resource (see :func:`mcp_resource`)."""
        return lambda fn: self.register_function(
            mcp_resource(uri, name, description, mime_type, **kwargs)(fn), only_last=True
        )

    def resource_template(self, uri_template: str, name: Optional[str] = None, description: Optional[str] = None,
                          mime_type: str = "text/plain", **kwargs: Any) -> Callable[[Any], Any]:
        """Decorator registering a function as a resource template (see :func:`mcp_resource_template`)."""
        return lambda fn: self.register_function(
            mcp_resource_template(uri_template, name, description, mime_type, **kwargs)(fn), only_last=True
        )

    def prompt(self, name: Any = None, description: Optional[str] = None, **kwargs: Any) -> Any:
        """Decorator registering a function as a prompt (see :func:`mcp_prompt`)."""
        if callable(name):
            return self.register_function(mcp_prompt(name), only_last=True)
        return lambda fn: self.register_function(mcp_prompt(name, description, **kwargs)(fn), only_last=True)

    # ---- discovery ---------------------------------------------------------------------------
    def register(self, *targets: Any) -> None:
        """Register modules, handler classes (instantiated with no args), instances or decorated functions."""
        for target in targets:
            if isinstance(target, types.ModuleType):
                self.register_module(target)
            elif inspect.isclass(target):
                try:
                    instance = target()
                except TypeError as exc:
                    raise McpConfigurationError(
                        f"Cannot instantiate handler class {target.__name__} without arguments; "
                        f"register an instance instead. ({exc})"
                    ) from exc
                self.register_instance(instance)
            elif callable(target) and get_specs(target):
                self.register_function(target)
            else:
                self.register_instance(target)

    def register_module(self, module: types.ModuleType) -> None:
        """Register decorated module-level functions and ``@mcp_handler`` / decorated classes in ``module``."""
        for _, member in inspect.getmembers(module):
            if getattr(member, "__module__", None) != module.__name__:
                continue
            if inspect.isfunction(member) and get_specs(member):
                self.register_function(member)
            elif inspect.isclass(member) and (is_mcp_handler(member) or self._class_has_specs(member)):
                self.register(member)

    @staticmethod
    def _class_has_specs(cls: type) -> bool:
        return any(get_specs(v) for v in vars(cls).values() if callable(v) or isinstance(v, (staticmethod, classmethod)))

    def register_instance(self, instance: Any) -> None:
        """Register all decorated methods of ``instance`` (bound methods, staticmethods and classmethods)."""
        cls = instance if inspect.isclass(instance) else type(instance)
        seen = set()
        for klass in cls.__mro__:
            for attr_name, raw in vars(klass).items():
                if attr_name in seen:
                    continue
                seen.add(attr_name)
                is_function_like = callable(raw) or isinstance(raw, (staticmethod, classmethod))
                if not is_function_like or inspect.isclass(raw) or not get_specs(raw):
                    continue
                self.register_function(getattr(instance, attr_name))

    def register_function(self, func: Any, only_last: bool = False) -> Any:
        """Register a callable carrying MCP decorator metadata. Returns ``func`` unchanged."""
        specs = get_specs(func)
        if not specs:
            raise McpConfigurationError(f"{func!r} has no MCP decorator metadata")
        callable_ = func.__func__ if isinstance(func, staticmethod) else func
        if isinstance(callable_, classmethod):
            raise McpConfigurationError("Register classmethods via their class or an instance")
        for spec in specs[-1:] if only_last else specs:
            if isinstance(spec, ToolSpec):
                self.add_tool(callable_, spec)
            elif isinstance(spec, ResourceSpec):
                self.add_resource(callable_, spec)
            elif isinstance(spec, ResourceTemplateSpec):
                self.add_resource_template(callable_, spec)
            elif isinstance(spec, PromptSpec):
                self.add_prompt(callable_, spec)
        return func

    # ---- low level add ----------------------------------------------------------------------
    def add_tool(self, func: Callable[..., Any], spec: ToolSpec) -> ToolRegistration:
        name = spec.name or func.__name__
        annotations: Optional[Dict[str, Any]] = None
        hints = (
            ("readOnlyHint", spec.read_only_hint),
            ("destructiveHint", spec.destructive_hint),
            ("idempotentHint", spec.idempotent_hint),
            ("openWorldHint", spec.open_world_hint),
        )
        if spec.title or any(v is not None for _, v in hints):
            annotations = {}
            if spec.title:
                annotations["title"] = spec.title
            for key, value in hints:
                if value is not None:
                    annotations[key] = value
        reg = ToolRegistration(
            func=func,
            icon_src=spec.icon_src,
            icon_mime_type=spec.icon_mime_type,
            name=name,
            description=spec.description if spec.description is not None else _doc(func),
            title=spec.title,
            parameters=_build_parameters(func, spec.params, "Parameter"),
            annotations=annotations,
            output_schema=_output_schema_for(func) if spec.use_structured_output else None,
            use_structured_output=spec.use_structured_output,
            meta=spec.meta,
        )
        self.tools[name] = reg
        logger.debug("Registered tool: %s", name)
        return reg

    def add_resource(self, func: Callable[..., Any], spec: ResourceSpec) -> Union[ResourceRegistration, ResourceTemplateRegistration]:
        if "{" in spec.uri and "}" in spec.uri:
            return self.add_resource_template(
                func,
                ResourceTemplateSpec(
                    uri_template=spec.uri, name=spec.name, description=spec.description, mime_type=spec.mime_type,
                    title=spec.title, icon_src=spec.icon_src, icon_mime_type=spec.icon_mime_type,
                ),
            )
        reg = ResourceRegistration(
            func=func,
            icon_src=spec.icon_src,
            icon_mime_type=spec.icon_mime_type,
            uri=spec.uri,
            name=spec.name or func.__name__,
            description=spec.description if spec.description is not None else _doc(func),
            mime_type=spec.mime_type,
            title=spec.title,
            parameters=_build_parameters(func, {}, "Parameter"),
        )
        self.resources[spec.uri] = reg
        logger.debug("Registered resource: %s", spec.uri)
        return reg

    def add_resource_template(self, func: Callable[..., Any], spec: ResourceTemplateSpec) -> ResourceTemplateRegistration:
        reg = ResourceTemplateRegistration(
            func=func,
            icon_src=spec.icon_src,
            icon_mime_type=spec.icon_mime_type,
            uri_template=spec.uri_template,
            name=spec.name or func.__name__,
            description=spec.description if spec.description is not None else _doc(func),
            mime_type=spec.mime_type,
            title=spec.title,
            parameters=_build_parameters(func, {}, "Parameter"),
            pattern=_build_template_regex(spec.uri_template),
        )
        self.resource_templates[spec.uri_template] = reg
        logger.debug("Registered resource template: %s", spec.uri_template)
        return reg

    def add_prompt(self, func: Callable[..., Any], spec: PromptSpec) -> PromptRegistration:
        name = spec.name or func.__name__
        reg = PromptRegistration(
            func=func,
            icon_src=spec.icon_src,
            icon_mime_type=spec.icon_mime_type,
            name=name,
            description=spec.description if spec.description is not None else _doc(func),
            title=spec.title,
            arguments=_build_parameters(func, spec.params, "Argument"),
        )
        self.prompts[name] = reg
        logger.debug("Registered prompt: %s", name)
        return reg

    # ---- invocation --------------------------------------------------------------------------
    @staticmethod
    def _bind_arguments(parameters: List[ParameterInfo], arguments: Any, token: CancellationToken, kind: str) -> Dict[str, Any]:
        arguments = arguments if isinstance(arguments, dict) else {}
        values: Dict[str, Any] = {}
        for p in parameters:
            if p.is_cancellation_token:
                values[p.name] = token
                continue
            found, raw = _lookup_ci(arguments, p.name)
            if found:
                try:
                    values[p.name] = convert_value(raw, p.annotation)
                except (TypeError, ValueError) as exc:
                    raise McpError.invalid_params(f"Invalid value for {kind} '{p.name}': {exc}") from exc
            elif not p.required:
                if p.default is not EMPTY:
                    values[p.name] = p.default
                else:
                    values[p.name] = None
            else:
                raise McpError.invalid_params(f"Required {kind} '{p.name}' is missing")
        return values

    def invoke_tool_raw(self, name: str, arguments: Any = None, token: Optional[CancellationToken] = None) -> Any:
        """Invoke a tool and return its raw Python return value (awaitables are resolved)."""
        tool = self.tools.get(name)
        if tool is None:
            raise McpError.invalid_params(f"Unknown tool: {name}")
        token = token or CancellationToken()
        values = self._bind_arguments(tool.parameters, arguments, token, "parameter")
        raw = _call(tool.func, tool.parameters, values)
        result = self.runner.resolve(raw, token)
        if inspect.isgenerator(result):
            result = list(result)
        return result

    def invoke_tool(self, name: str, arguments: Any = None, token: Optional[CancellationToken] = None) -> str:
        """Invoke a tool and return a string representation of the result."""
        return _to_text(self.invoke_tool_raw(name, arguments, token))

    def read_resource(self, uri: str, token: Optional[CancellationToken] = None) -> Tuple[Union[str, bytes], str]:
        """Read a static resource or a resource template match. Returns ``(text_or_bytes, mime_type)``."""
        token = token or CancellationToken()
        resource = self.resources.get(uri)
        if resource is not None:
            values = {p.name: token for p in resource.parameters if p.is_cancellation_token}
            raw = _call(resource.func, resource.parameters, values)
            return self._resource_content(self.runner.resolve(raw, token)), resource.mime_type

        for template in self.resource_templates.values():
            match = template.pattern.match(uri) if template.pattern else None
            if not match:
                continue
            groups = match.groupdict()
            values = {}
            for p in template.parameters:
                if p.is_cancellation_token:
                    values[p.name] = token
                elif groups.get(p.name) is not None:
                    raw_value = urllib.parse.unquote(groups[p.name])
                    try:
                        values[p.name] = convert_string(raw_value, p.annotation)
                    except (TypeError, ValueError) as exc:
                        raise McpError.invalid_params(f"Invalid URI template parameter '{p.name}': {exc}") from exc
                elif not p.required:
                    values[p.name] = p.default if p.default is not EMPTY else None
                else:
                    raise McpError.invalid_params(f"Missing URI template parameter '{p.name}'")
            raw = _call(template.func, template.parameters, values)
            return self._resource_content(self.runner.resolve(raw, token)), template.mime_type

        raise McpError.invalid_params(f"Resource not found: {uri}")

    @staticmethod
    def _resource_content(value: Any) -> Union[str, bytes]:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return _to_text(value)

    def get_prompt_raw(self, name: str, arguments: Any = None, token: Optional[CancellationToken] = None) -> Any:
        prompt = self.prompts.get(name)
        if prompt is None:
            raise McpError.invalid_params(f"Unknown prompt: {name}")
        token = token or CancellationToken()
        values = self._bind_arguments(prompt.arguments, arguments, token, "argument")
        raw = _call(prompt.func, prompt.arguments, values)
        return self.runner.resolve(raw, token)

    def get_prompt(self, name: str, arguments: Any = None, token: Optional[CancellationToken] = None) -> str:
        """Render a prompt and return it as text."""
        return _to_text(self.get_prompt_raw(name, arguments, token))

    # ---- list responses ----------------------------------------------------------------------
    @staticmethod
    def _with_cache(key: str, items: List[Any], ttl_ms: Optional[int], cache_scope: str) -> Dict[str, Any]:
        if ttl_ms is not None:
            return {key: items, "ttlMs": ttl_ms, "cacheScope": cache_scope}
        return {key: items}

    def tools_list(self, base_url: Optional[str] = None, ttl_ms: Optional[int] = None, cache_scope: str = "public") -> Dict[str, Any]:
        items = []
        for t in self.tools.values():
            visible = [p for p in t.parameters if not p.is_cancellation_token]
            entry: Dict[str, Any] = {
                "name": t.name,
                "description": t.description,
                "inputSchema": {
                    "type": "object",
                    "properties": {p.name: p.schema() for p in visible},
                    "required": [p.name for p in visible if p.required],
                },
            }
            if t.title:
                entry["title"] = t.title
            if t.annotations:
                entry["annotations"] = dict(t.annotations)
            if t.output_schema is not None:
                entry["outputSchema"] = t.output_schema
            icons = t.icons
            if icons:
                entry["icons"] = resolve_icons(icons, base_url)
            if t.meta:
                entry["_meta"] = dict(t.meta)
            items.append(entry)
        return self._with_cache("tools", items, ttl_ms, cache_scope)

    def resources_list(self, base_url: Optional[str] = None, ttl_ms: Optional[int] = None, cache_scope: str = "public") -> Dict[str, Any]:
        items = []
        for r in self.resources.values():
            entry: Dict[str, Any] = {"uri": r.uri, "name": r.name, "description": r.description, "mimeType": r.mime_type}
            if r.title:
                entry["title"] = r.title
            icons = r.icons
            if icons:
                entry["icons"] = resolve_icons(icons, base_url)
            items.append(entry)
        return self._with_cache("resources", items, ttl_ms, cache_scope)

    def resource_templates_list(self, base_url: Optional[str] = None, ttl_ms: Optional[int] = None, cache_scope: str = "public") -> Dict[str, Any]:
        items = []
        for rt in self.resource_templates.values():
            entry: Dict[str, Any] = {
                "uriTemplate": rt.uri_template, "name": rt.name, "description": rt.description, "mimeType": rt.mime_type,
            }
            if rt.title:
                entry["title"] = rt.title
            icons = rt.icons
            if icons:
                entry["icons"] = resolve_icons(icons, base_url)
            items.append(entry)
        return self._with_cache("resourceTemplates", items, ttl_ms, cache_scope)

    def prompts_list(self, base_url: Optional[str] = None, ttl_ms: Optional[int] = None, cache_scope: str = "public") -> Dict[str, Any]:
        items = []
        for p in self.prompts.values():
            entry: Dict[str, Any] = {
                "name": p.name,
                "description": p.description,
                "arguments": [
                    {"name": a.name, "description": a.description, "required": a.required}
                    for a in p.arguments if not a.is_cancellation_token
                ],
            }
            if p.title:
                entry["title"] = p.title
            icons = p.icons
            if icons:
                entry["icons"] = resolve_icons(icons, base_url)
            items.append(entry)
        return self._with_cache("prompts", items, ttl_ms, cache_scope)
