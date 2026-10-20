"""A small, dependency-free JSON Schema subset validator.

Supports: type (incl. union lists), required, properties, additionalProperties
(bool), enum, minimum, maximum, minLength, items, minItems, maxItems. That is
enough to pin down LLM output contracts without pulling in a dependency.
"""

from __future__ import annotations

from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


class SchemaError(ValueError):
    def __init__(self, message: str, path: str) -> None:
        super().__init__(f"{path or '<root>'}: {message}")
        self.path = path
        self.reason = message


def _is_type(value: Any, name: str) -> bool:
    if name in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def validate(instance: Any, schema: dict[str, Any], path: str = "") -> None:
    """Raise SchemaError on the first violation found."""
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(instance, n) for n in names):
            raise SchemaError(f"expected {'/'.join(names)}, got {type(instance).__name__}", path)

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{instance!r} not in {schema['enum']}", path)

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(f"{instance} < minimum {schema['minimum']}", path)
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(f"{instance} > maximum {schema['maximum']}", path)

    if isinstance(instance, str) and len(instance) < schema.get("minLength", 0):
        raise SchemaError(f"string shorter than {schema['minLength']}", path)

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                raise SchemaError(f"missing required property '{key}'", path)
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(props))
            if extra:
                raise SchemaError(f"unexpected properties {extra}", path)
        for key, sub in props.items():
            if key in instance:
                validate(instance[key], sub, f"{path}.{key}" if path else key)

    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            raise SchemaError(f"fewer than {schema['minItems']} items", path)
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaError(f"more than {schema['maxItems']} items", path)
        if "items" in schema:
            for i, item in enumerate(instance):
                validate(item, schema["items"], f"{path}[{i}]")
