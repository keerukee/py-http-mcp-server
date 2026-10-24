"""Minimal, dependency-free JSON helpers.

``to_jsonable`` recursively converts arbitrary Python values (dataclasses, enums,
named tuples, datetimes, UUIDs, ``McpContent`` objects, ...) into plain JSON types.
"""

import base64
import dataclasses
import datetime
import decimal
import enum
import json
import math
import uuid
from typing import Any

__all__ = ["to_jsonable", "dumps", "loads", "is_structured_value"]

_PRIMITIVE_SCALARS = (
    str, bytes, bytearray, bool, int, float, decimal.Decimal,
    datetime.datetime, datetime.date, datetime.time, datetime.timedelta,
    uuid.UUID, enum.Enum,
)


def _is_namedtuple(value: Any) -> bool:
    return isinstance(value, tuple) and hasattr(value, "_asdict") and hasattr(value, "_fields")


def to_jsonable(value: Any) -> Any:
    """Recursively convert ``value`` into JSON-serializable primitives."""
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, enum.Enum):  # IntEnum / StrEnum are int/str subclasses
            return to_jsonable(value.value)
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {(k if isinstance(k, str) else str(to_jsonable(k))): to_jsonable(v) for k, v in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict) and not isinstance(value, type):
        return to_jsonable(to_dict())
    if _is_namedtuple(value):
        return to_jsonable(value._asdict())
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() and value.is_finite() else float(value)
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    model_dump = getattr(value, "model_dump", None)  # pydantic v2 style objects (optional, duck-typed)
    if callable(model_dump):
        try:
            return to_jsonable(model_dump(mode="json"))
        except TypeError:
            return to_jsonable(model_dump())
    if hasattr(value, "__iter__") and not hasattr(value, "__dict__"):
        try:
            return [to_jsonable(v) for v in value]
        except TypeError:
            pass
    if hasattr(value, "__dict__") and not callable(value):
        return {k: to_jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def dumps(value: Any) -> str:
    """Serialize ``value`` to compact JSON text."""
    return json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":"))


def loads(text: Any) -> Any:
    """Parse JSON from ``str`` or ``bytes`` (UTF-8, BOM tolerated)."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8-sig")
    elif isinstance(text, str) and text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(text)


def is_structured_value(value: Any) -> bool:
    """True if ``value`` is an object-like result that maps to MCP ``structuredContent``."""
    from .content import McpContent, McpToolResult  # local import to avoid cycles

    if value is None or isinstance(value, _PRIMITIVE_SCALARS):
        return False
    if isinstance(value, (McpContent, McpToolResult)):
        return False
    if isinstance(value, dict) or _is_namedtuple(value):
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        return False
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return True
    if callable(getattr(value, "model_dump", None)):
        return True
    return hasattr(value, "__dict__") and not callable(value) and not isinstance(value, type)
