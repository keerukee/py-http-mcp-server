"""Decorators that mark functions/methods as MCP tools, resources, resource templates and prompts.

These are the Python equivalents of the .NET attributes::

    [McpHandler]          -> @mcp_handler
    [McpTool]             -> @mcp_tool
    [McpResource]         -> @mcp_resource
    [McpResourceTemplate] -> @mcp_resource_template
    [McpPrompt]           -> @mcp_prompt
    [McpIcon]             -> @mcp_icon
    [McpParameter]        -> Annotated[T, Param("description", enum=[...])]  (or params={...})
    [McpArgument]         -> Annotated[T, Param("description")]
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TypeVar, Union

from .content import McpIcon

__all__ = [
    "Param",
    "McpParameter",
    "McpArgument",
    "ToolSpec",
    "ResourceSpec",
    "ResourceTemplateSpec",
    "PromptSpec",
    "mcp_handler",
    "mcp_tool",
    "mcp_resource",
    "mcp_resource_template",
    "mcp_prompt",
    "mcp_icon",
    "get_specs",
    "get_icons",
    "is_mcp_handler",
    "MCP_SPECS_ATTR",
    "MCP_ICONS_ATTR",
    "MCP_HANDLER_ATTR",
]

F = TypeVar("F")

MCP_SPECS_ATTR = "__mcp_specs__"
MCP_ICONS_ATTR = "__mcp_icons__"
MCP_HANDLER_ATTR = "__mcp_handler__"


@dataclass(frozen=True)
class Param:
    """Metadata for a tool parameter / prompt argument.

    Use with ``typing.Annotated``::

        def get_weather(city: Annotated[str, Param("City name", enum=["Seattle", "London"])]): ...
    """

    description: Optional[str] = None
    enum: Optional[Sequence[Any]] = None


#: Aliases matching the .NET attribute names.
McpParameter = Param
McpArgument = Param

ParamsArg = Optional[Mapping[str, Union[str, Param]]]


@dataclass
class ToolSpec:
    name: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = None
    read_only_hint: Optional[bool] = None
    destructive_hint: Optional[bool] = None
    idempotent_hint: Optional[bool] = None
    open_world_hint: Optional[bool] = None
    use_structured_output: bool = False
    icon_src: Optional[str] = None
    icon_mime_type: Optional[str] = None
    params: Dict[str, Param] = field(default_factory=dict)
    meta: Optional[Dict[str, Any]] = None


@dataclass
class ResourceSpec:
    uri: str
    name: Optional[str] = None
    description: Optional[str] = None
    mime_type: str = "text/plain"
    title: Optional[str] = None
    icon_src: Optional[str] = None
    icon_mime_type: Optional[str] = None


@dataclass
class ResourceTemplateSpec:
    uri_template: str
    name: Optional[str] = None
    description: Optional[str] = None
    mime_type: str = "text/plain"
    title: Optional[str] = None
    icon_src: Optional[str] = None
    icon_mime_type: Optional[str] = None


@dataclass
class PromptSpec:
    name: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = None
    icon_src: Optional[str] = None
    icon_mime_type: Optional[str] = None
    params: Dict[str, Param] = field(default_factory=dict)


Spec = Union[ToolSpec, ResourceSpec, ResourceTemplateSpec, PromptSpec]


# ---------------------------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------------------------
def _underlying(fn: Any) -> Any:
    """Return the plain function behind staticmethod/classmethod/bound-method wrappers."""
    while isinstance(fn, (staticmethod, classmethod)) or hasattr(fn, "__func__"):
        fn = fn.__func__
    return fn


def _attach(fn: F, spec: Spec) -> F:
    target = _underlying(fn)
    specs = target.__dict__.get(MCP_SPECS_ATTR)
    if specs is None:
        specs = []
        setattr(target, MCP_SPECS_ATTR, specs)
    specs.append(spec)
    return fn


def _normalize_params(params: ParamsArg) -> Dict[str, Param]:
    out: Dict[str, Param] = {}
    for key, value in (params or {}).items():
        out[key] = value if isinstance(value, Param) else Param(description=str(value))
    return out


def get_specs(fn: Any) -> List[Spec]:
    """Return MCP specs attached to ``fn`` (empty list if none)."""
    return list(getattr(_underlying(fn), MCP_SPECS_ATTR, None) or [])


def get_icons(fn: Any) -> List[McpIcon]:
    """Return icons attached via ``@mcp_icon`` (top-to-bottom decorator order)."""
    return list(getattr(_underlying(fn), MCP_ICONS_ATTR, None) or [])


def is_mcp_handler(obj: Any) -> bool:
    return bool(getattr(obj, MCP_HANDLER_ATTR, False))


def _decorator(spec_factory: Callable[[], Spec], target: Any = None) -> Any:
    def wrap(fn: F) -> F:
        return _attach(fn, spec_factory())

    return wrap(target) if target is not None else wrap


# ---------------------------------------------------------------------------------------------
# Public decorators
# ---------------------------------------------------------------------------------------------
def mcp_handler(cls: F) -> F:
    """Mark a class as an MCP handler container (equivalent of ``[McpHandler]``)."""
    setattr(cls, MCP_HANDLER_ATTR, True)
    return cls


def mcp_tool(
    name: Any = None,
    description: Optional[str] = None,
    *,
    title: Optional[str] = None,
    read_only_hint: Optional[bool] = None,
    destructive_hint: Optional[bool] = None,
    idempotent_hint: Optional[bool] = None,
    open_world_hint: Optional[bool] = None,
    use_structured_output: bool = False,
    icon_src: Optional[str] = None,
    icon_mime_type: Optional[str] = None,
    params: ParamsArg = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Any:
    """Mark a function as an MCP tool. ``name`` defaults to the function name, ``description`` to its docstring.

    Can be used bare (``@mcp_tool``) or with arguments (``@mcp_tool("name", "description", ...)``).
    """
    target = name if callable(name) or isinstance(name, (staticmethod, classmethod)) else None
    tool_name = None if target is not None else name
    return _decorator(
        lambda: ToolSpec(
            name=tool_name,
            description=description,
            title=title,
            read_only_hint=read_only_hint,
            destructive_hint=destructive_hint,
            idempotent_hint=idempotent_hint,
            open_world_hint=open_world_hint,
            use_structured_output=use_structured_output,
            icon_src=icon_src,
            icon_mime_type=icon_mime_type,
            params=_normalize_params(params),
            meta=meta,
        ),
        target,
    )


def mcp_resource(
    uri: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    mime_type: str = "text/plain",
    *,
    title: Optional[str] = None,
    icon_src: Optional[str] = None,
    icon_mime_type: Optional[str] = None,
) -> Callable[[F], F]:
    """Mark a function as a static MCP resource. URIs containing ``{var}`` become resource templates."""
    return _decorator(
        lambda: ResourceSpec(
            uri=uri, name=name, description=description, mime_type=mime_type,
            title=title, icon_src=icon_src, icon_mime_type=icon_mime_type,
        )
    )


def mcp_resource_template(
    uri_template: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    mime_type: str = "text/plain",
    *,
    title: Optional[str] = None,
    icon_src: Optional[str] = None,
    icon_mime_type: Optional[str] = None,
) -> Callable[[F], F]:
    """Mark a function as a parameterized resource template (e.g. ``"users://{id}/profile"``)."""
    return _decorator(
        lambda: ResourceTemplateSpec(
            uri_template=uri_template, name=name, description=description, mime_type=mime_type,
            title=title, icon_src=icon_src, icon_mime_type=icon_mime_type,
        )
    )


def mcp_prompt(
    name: Any = None,
    description: Optional[str] = None,
    *,
    title: Optional[str] = None,
    icon_src: Optional[str] = None,
    icon_mime_type: Optional[str] = None,
    params: ParamsArg = None,
) -> Any:
    """Mark a function as an MCP prompt. Can be used bare (``@mcp_prompt``) or with arguments."""
    target = name if callable(name) or isinstance(name, (staticmethod, classmethod)) else None
    prompt_name = None if target is not None else name
    return _decorator(
        lambda: PromptSpec(
            name=prompt_name, description=description, title=title,
            icon_src=icon_src, icon_mime_type=icon_mime_type, params=_normalize_params(params),
        ),
        target,
    )


def mcp_icon(
    src: str,
    mime_type: Optional[str] = None,
    theme: Optional[str] = None,
    *sizes: str,
) -> Callable[[F], F]:
    """Attach an icon to a tool/resource/template/prompt (stackable; equivalent of ``[McpIcon]``)."""
    icon = McpIcon(src=src, mime_type=mime_type, theme=theme, sizes=list(sizes) or None)

    def wrap(fn: F) -> F:
        target = _underlying(fn)
        icons = target.__dict__.get(MCP_ICONS_ATTR)
        if icons is None:
            icons = []
            setattr(target, MCP_ICONS_ATTR, icons)
        icons.insert(0, icon)  # decorators apply bottom-up; keep visual top-to-bottom order
        return fn

    return wrap
