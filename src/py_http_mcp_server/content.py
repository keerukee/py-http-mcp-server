"""Rich content types: :class:`McpContent`, :class:`McpToolResult` and :class:`McpIcon`."""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import _json

__all__ = ["McpContent", "McpToolResult", "McpIcon", "resolve_icons"]


@dataclass
class McpContent:
    """A content block returned by a tool or prompt (``text``, ``image``, ``audio``, ``resource_link``, ``resource``)."""

    type: str
    text: Optional[str] = None
    data: Optional[str] = None
    mime_type: Optional[str] = None
    uri: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    resource: Optional[Dict[str, Any]] = None
    annotations: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type}
        for key, value in (
            ("text", self.text),
            ("data", self.data),
            ("mimeType", self.mime_type),
            ("uri", self.uri),
            ("name", self.name),
            ("description", self.description),
            ("resource", self.resource),
            ("annotations", self.annotations),
        ):
            if value is not None:
                out[key] = value
        return out

    @classmethod
    def create_text(cls, text: str) -> "McpContent":
        """Create a standard text content block."""
        return cls(type="text", text=text)

    @classmethod
    def create_image(cls, base64_data: str, mime_type: str = "image/png") -> "McpContent":
        """Create a base64-encoded image content block."""
        return cls(type="image", data=base64_data, mime_type=mime_type)

    @classmethod
    def create_audio(cls, base64_data: str, mime_type: str = "audio/wav") -> "McpContent":
        """Create a base64-encoded audio content block (MCP 2025-03-26+)."""
        return cls(type="audio", data=base64_data, mime_type=mime_type)

    @classmethod
    def create_resource_link(
        cls,
        uri: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> "McpContent":
        """Create a resource link content block (MCP 2025-06-18+)."""
        return cls(type="resource_link", uri=uri, name=name, description=description, mime_type=mime_type)

    @classmethod
    def create_embedded_resource(
        cls,
        uri: str,
        text: Optional[str] = None,
        mime_type: str = "text/plain",
        blob: Optional[str] = None,
    ) -> "McpContent":
        """Create an embedded resource content block (``text`` or base64 ``blob``)."""
        resource: Dict[str, Any] = {"uri": uri, "mimeType": mime_type}
        if blob is not None:
            resource["blob"] = blob
        else:
            resource["text"] = text if text is not None else ""
        return cls(type="resource", resource=resource)


@dataclass
class McpToolResult:
    """A full tool result: multiple content blocks, optional ``structuredContent`` and an error flag."""

    content: List[McpContent] = field(default_factory=list)
    structured_content: Any = None
    is_error: bool = False

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"content": [c.to_dict() for c in self.content]}
        if self.structured_content is not None:
            out["structuredContent"] = _json.to_jsonable(self.structured_content)
        out["isError"] = self.is_error
        return out

    @classmethod
    def success(cls, *content: McpContent) -> "McpToolResult":
        return cls(content=list(content), is_error=False)

    @classmethod
    def structured(cls, structured_data: Any, fallback_text: Optional[str] = None) -> "McpToolResult":
        text = fallback_text if fallback_text is not None else _json.dumps(structured_data)
        return cls(content=[McpContent.create_text(text)], structured_content=structured_data, is_error=False)

    @classmethod
    def error(cls, error_message: str) -> "McpToolResult":
        return cls(content=[McpContent.create_text(error_message)], is_error=True)


@dataclass
class McpIcon:
    """Icon metadata for implementations, tools, resources and prompts (MCP SEP-973).

    Relative ``src`` paths (e.g. ``"/icon.png"``) are resolved against the incoming request's base URL.
    """

    src: str
    mime_type: Optional[str] = None
    sizes: Optional[Sequence[str]] = None
    theme: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"src": self.src}
        if self.mime_type is not None:
            out["mimeType"] = self.mime_type
        if self.sizes:
            out["sizes"] = list(self.sizes)
        if self.theme is not None:
            out["theme"] = self.theme
        return out

    def resolved(self, base_url: str) -> "McpIcon":
        src = self.src
        lowered = (src or "").lower()
        if src and not lowered.startswith(("http://", "https://", "data:")):
            src = f"{base_url}{src}" if src.startswith("/") else f"{base_url}/{src}"
        return McpIcon(src=src, mime_type=self.mime_type, sizes=self.sizes, theme=self.theme)


def resolve_icons(icons: Iterable[McpIcon], base_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """Resolve relative icon paths to absolute URLs so strict MCP clients pass URL validation."""
    base = (base_url or "http://localhost").rstrip("/")
    return [icon.resolved(base).to_dict() for icon in icons]
