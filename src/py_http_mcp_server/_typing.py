"""Type-hint introspection: JSON Schema generation and JSON -> Python argument conversion.

Supports: ``str``, ``int``, ``float``, ``bool``, ``Decimal``, ``datetime``/``date``/``time``, ``UUID``,
``bytes`` (base64), ``Enum``, ``Literal``, ``Optional``/``Union`` (incl. ``X | None``), ``list``/``tuple``/
``set``/``Sequence``, ``dict``/``Mapping``, dataclasses, ``TypedDict``, ``NamedTuple`` and ``Annotated``.
"""

import base64
import collections.abc
import dataclasses
import datetime
import decimal
import enum
import inspect
import json
import typing
import uuid
from typing import Any, Dict, Optional, Tuple, Union, get_args, get_origin

try:  # Python 3.10+
    from types import UnionType as _UnionType  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Python 3.9
    _UnionType = None  # type: ignore[assignment]

__all__ = [
    "EMPTY",
    "unwrap_annotated",
    "unwrap_optional",
    "unwrap_awaitable",
    "json_schema",
    "convert_value",
    "convert_string",
    "type_hints",
]

EMPTY = inspect.Parameter.empty
_NoneType = type(None)

_ARRAY_ORIGINS = (
    list, tuple, set, frozenset,
    collections.abc.Sequence, collections.abc.MutableSequence,
    collections.abc.Set, collections.abc.MutableSet,
    collections.abc.Collection, collections.abc.Iterable,
)
_SET_ORIGINS = (set, frozenset, collections.abc.Set, collections.abc.MutableSet)
_MAP_ORIGINS = (dict, collections.abc.Mapping, collections.abc.MutableMapping)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def type_hints(obj: Any) -> Dict[str, Any]:
    """``typing.get_type_hints`` with ``Annotated`` extras, falling back to raw ``__annotations__``."""
    try:
        return typing.get_type_hints(obj, include_extras=True)
    except Exception:
        return dict(getattr(obj, "__annotations__", None) or {})


def unwrap_annotated(tp: Any) -> Tuple[Any, Tuple[Any, ...]]:
    metadata: Tuple[Any, ...] = ()
    while get_origin(tp) is typing.Annotated:
        args = get_args(tp)
        tp, metadata = args[0], metadata + tuple(args[1:])
    return tp, metadata


def _is_union(tp: Any) -> bool:
    origin = get_origin(tp)
    return origin is Union or (_UnionType is not None and origin is _UnionType)


def unwrap_optional(tp: Any) -> Tuple[Any, bool]:
    """Return ``(inner_type, is_optional)`` for ``Optional[X]`` / ``X | None``."""
    if _is_union(tp):
        args = get_args(tp)
        non_none = tuple(a for a in args if a is not _NoneType)
        if len(non_none) < len(args):
            if len(non_none) == 1:
                return non_none[0], True
            return Union[non_none], True  # type: ignore[return-value]
    return tp, False


def unwrap_awaitable(tp: Any) -> Any:
    """``Awaitable[X]`` / ``Coroutine[..., X]`` -> ``X``."""
    origin = get_origin(tp)
    if origin in (collections.abc.Awaitable, collections.abc.Coroutine):
        args = get_args(tp)
        return args[-1] if args else Any
    return tp


def _is_enum(tp: Any) -> bool:
    return inspect.isclass(tp) and issubclass(tp, enum.Enum)


def _is_typeddict(tp: Any) -> bool:
    return inspect.isclass(tp) and issubclass(tp, dict) and hasattr(tp, "__total__")


def _is_namedtuple_type(tp: Any) -> bool:
    return inspect.isclass(tp) and issubclass(tp, tuple) and hasattr(tp, "_fields")


def _is_dataclass_type(tp: Any) -> bool:
    return inspect.isclass(tp) and dataclasses.is_dataclass(tp)


def _type_name(tp: Any) -> str:
    return getattr(tp, "__name__", None) or str(tp)


# ---------------------------------------------------------------------------------------------
# JSON Schema generation
# ---------------------------------------------------------------------------------------------
def json_schema(tp: Any, _seen: Optional[set] = None) -> Dict[str, Any]:
    """Generate a JSON Schema fragment for a Python type annotation."""
    tp, _ = unwrap_annotated(tp)
    tp, _ = unwrap_optional(tp)
    return _schema_core(tp, _seen if _seen is not None else set())


def _object_schema(properties: Dict[str, Any], required: list) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def _schema_core(tp: Any, seen: set) -> Dict[str, Any]:
    if tp is EMPTY or tp is Any or tp is None:
        return {"type": "string"}
    if tp is object:
        return {}
    origin, args = get_origin(tp), get_args(tp)

    if tp is str:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp in (float, decimal.Decimal):
        return {"type": "number"}
    if tp is datetime.datetime:
        return {"type": "string", "format": "date-time"}
    if tp is datetime.date:
        return {"type": "string", "format": "date"}
    if tp is datetime.time:
        return {"type": "string", "format": "time"}
    if tp is uuid.UUID:
        return {"type": "string", "format": "uuid"}
    if tp in (bytes, bytearray):
        return {"type": "string", "contentEncoding": "base64"}
    if _is_enum(tp):
        return {"type": "string", "enum": [m.name for m in tp]}
    if origin is typing.Literal:
        values = list(args)
        schema: Dict[str, Any] = {"enum": values}
        if all(isinstance(v, str) for v in values):
            schema["type"] = "string"
        elif all(isinstance(v, bool) for v in values):
            schema["type"] = "boolean"
        elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            schema["type"] = "integer"
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            schema["type"] = "number"
        return schema
    if _is_union(tp):
        return {"anyOf": [json_schema(a, seen) for a in args if a is not _NoneType]}

    if tp in (list, tuple, set, frozenset) or origin in _ARRAY_ORIGINS:
        schema = {"type": "array"}
        if args:
            item_tp = args[0]
            if origin is tuple and not (len(args) == 2 and args[1] is Ellipsis) and len(set(args)) > 1:
                return schema
            schema["items"] = json_schema(item_tp, seen)
        return schema
    if tp is dict or origin in _MAP_ORIGINS:
        schema = {"type": "object"}
        if len(args) == 2 and args[1] is not Any:
            schema["additionalProperties"] = json_schema(args[1], seen)
        return schema

    if inspect.isclass(tp):
        if tp in seen:
            return {"type": "object"}
        seen = seen | {tp}
        if _is_dataclass_type(tp):
            hints = type_hints(tp)
            props, required = {}, []
            for f in dataclasses.fields(tp):
                if not f.init:
                    continue
                f_tp = hints.get(f.name, Any)
                props[f.name] = json_schema(f_tp, seen)
                no_default = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING  # type: ignore[misc]
                if no_default and not unwrap_optional(unwrap_annotated(f_tp)[0])[1]:
                    required.append(f.name)
            return _object_schema(props, required)
        if _is_typeddict(tp):
            hints = type_hints(tp)
            req_keys = getattr(tp, "__required_keys__", set(hints) if tp.__total__ else set())
            props = {k: json_schema(v, seen) for k, v in hints.items()}
            return _object_schema(props, [k for k in hints if k in req_keys])
        if _is_namedtuple_type(tp):
            hints = type_hints(tp)
            defaults = getattr(tp, "_field_defaults", {})
            props = {k: json_schema(hints.get(k, Any), seen) for k in tp._fields}
            return _object_schema(props, [k for k in tp._fields if k not in defaults])
        annotations = type_hints(tp) if tp.__module__ != "builtins" else {}
        if annotations:
            props = {k: json_schema(v, seen) for k, v in annotations.items() if not k.startswith("_")}
            required = [
                k for k, v in annotations.items()
                if not k.startswith("_") and not hasattr(tp, k) and not unwrap_optional(unwrap_annotated(v)[0])[1]
            ]
            return _object_schema(props, required)
    return {"type": "object"}


# ---------------------------------------------------------------------------------------------
# Value conversion (JSON -> Python)
# ---------------------------------------------------------------------------------------------
def convert_value(value: Any, tp: Any) -> Any:
    """Convert a decoded JSON ``value`` to the annotated Python type ``tp``."""
    if tp is EMPTY or tp is Any or tp is object or tp is None:
        return value
    tp, _ = unwrap_annotated(tp)
    inner, _ = unwrap_optional(tp)
    if value is None:
        return None
    return _convert(value, inner)


def convert_string(raw: str, tp: Any) -> Any:
    """Convert a raw string (e.g. a URI-template variable) to type ``tp``."""
    if tp is EMPTY or tp is Any or tp is object or tp is None:
        return raw
    tp, _ = unwrap_annotated(tp)
    inner, _ = unwrap_optional(tp)
    return _convert_str(raw, inner)


def _parse_datetime(raw: str) -> datetime.datetime:
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(text)


def _convert_enum(value: Any, tp: Any) -> Any:
    if isinstance(value, tp):
        return value
    if isinstance(value, str):
        for member in tp:
            if member.name.lower() == value.lower():
                return member
    try:
        return tp(value)
    except ValueError:
        pass
    if isinstance(value, str):
        for member in tp:
            if str(member.value).lower() == value.lower():
                return member
    raise ValueError(f"'{value}' is not a valid {_type_name(tp)}; expected one of {[m.name for m in tp]}")


def _convert_str(raw: str, tp: Any) -> Any:
    if tp is str or tp is EMPTY or tp is Any or tp is object:
        return raw
    if _is_union(tp) or get_origin(tp) is typing.Literal:
        return _convert(raw, tp)
    if tp is bool:
        lowered = raw.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise ValueError(f"'{raw}' is not a valid boolean")
    if tp is int:
        return int(raw.strip())
    if tp is float:
        return float(raw.strip())
    if tp is decimal.Decimal:
        try:
            return decimal.Decimal(raw.strip())
        except decimal.InvalidOperation:
            raise ValueError(f"'{raw}' is not a valid decimal") from None
    if tp is uuid.UUID:
        return uuid.UUID(raw.strip())
    if tp is datetime.datetime:
        return _parse_datetime(raw)
    if tp is datetime.date:
        return datetime.date.fromisoformat(raw.strip())
    if tp is datetime.time:
        return datetime.time.fromisoformat(raw.strip())
    if tp in (bytes, bytearray):
        return tp(base64.b64decode(raw))
    if _is_enum(tp):
        return _convert_enum(raw, tp)
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ValueError(f"Cannot convert '{raw}' to {_type_name(tp)}") from None
    return _convert(parsed, tp)


def _convert(value: Any, tp: Any) -> Any:  # noqa: C901 - a flat dispatch is clearest here
    if tp is Any or tp is object or tp is EMPTY:
        return value
    origin, args = get_origin(tp), get_args(tp)

    if _is_union(tp):
        errors = []
        for arg in args:
            if arg is _NoneType:
                continue
            try:
                return convert_value(value, arg)
            except (TypeError, ValueError, KeyError) as exc:
                errors.append(str(exc))
        raise ValueError(f"Value {value!r} does not match {tp}")
    if origin is typing.Literal:
        for arg in args:
            if value == arg and type(value) is type(arg):
                return arg
            if isinstance(value, str) and not isinstance(arg, str):
                try:
                    if _convert_str(value, type(arg)) == arg:
                        return arg
                except (TypeError, ValueError):
                    pass
        raise ValueError(f"Value {value!r} is not one of {list(args)}")

    if isinstance(value, str) and tp is not str and not _is_enum(tp):
        return _convert_str(value, tp)

    if tp is str:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if tp is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise TypeError(f"Expected a boolean, got {value!r}")
    if tp is int:
        if isinstance(value, bool):
            raise TypeError(f"Expected an integer, got {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise TypeError(f"Expected an integer, got {value!r}")
    if tp is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise TypeError(f"Expected a number, got {value!r}")
    if tp is decimal.Decimal:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return decimal.Decimal(str(value))
        raise TypeError(f"Expected a number, got {value!r}")
    if _is_enum(tp):
        return _convert_enum(value, tp)
    if tp is datetime.datetime and isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)

    if tp in (list, tuple, set, frozenset) or origin in _ARRAY_ORIGINS:
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"Expected an array, got {type(value).__name__}")
        container = origin or tp
        if args and container is tuple and not (len(args) == 2 and args[1] is Ellipsis):
            if len(args) != len(value):
                raise ValueError(f"Expected an array of {len(args)} items, got {len(value)}")
            return tuple(convert_value(v, a) for v, a in zip(value, args))
        item_tp = args[0] if args else Any
        items = [convert_value(v, item_tp) for v in value]
        if container in _SET_ORIGINS:
            return frozenset(items) if container is frozenset else set(items)
        if container is tuple:
            return tuple(items)
        return items
    if tp is dict or origin in _MAP_ORIGINS:
        if not isinstance(value, dict):
            raise TypeError(f"Expected an object, got {type(value).__name__}")
        if len(args) == 2:
            return {convert_string(k, args[0]): convert_value(v, args[1]) for k, v in value.items()}
        return dict(value)

    if inspect.isclass(tp):
        if _is_dataclass_type(tp) and isinstance(value, dict):
            return _build_from_mapping(tp, value, {f.name: f for f in dataclasses.fields(tp) if f.init})
        if _is_typeddict(tp) and isinstance(value, dict):
            hints = type_hints(tp)
            return {k: (convert_value(v, hints[k]) if k in hints else v) for k, v in value.items()}
        if _is_namedtuple_type(tp):
            hints = type_hints(tp)
            if isinstance(value, dict):
                return _build_from_mapping(tp, value, {name: None for name in tp._fields})
            if isinstance(value, (list, tuple)):
                return tp(*(convert_value(v, hints.get(n, Any)) for v, n in zip(value, tp._fields)))
        model_validate = getattr(tp, "model_validate", None)
        if callable(model_validate):
            return model_validate(value)
        if isinstance(value, tp):
            return value
        if isinstance(value, dict):
            return tp(**value)
    return value


def _build_from_mapping(tp: Any, value: Dict[str, Any], fields: Dict[str, Any]) -> Any:
    hints = type_hints(tp)
    lowered = {k.lower(): k for k in value}
    kwargs = {}
    for name, f in fields.items():
        key = name if name in value else lowered.get(name.lower())
        if key is None:
            has_default = (
                f is None and name in getattr(tp, "_field_defaults", {})
            ) or (
                f is not None and (f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING)
            )
            if has_default:
                continue
            raise ValueError(f"Missing required field '{name}' for {_type_name(tp)}")
        kwargs[name] = convert_value(value[key], hints.get(name, Any))
    return tp(**kwargs)
