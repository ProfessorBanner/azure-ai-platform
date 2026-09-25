"""Function schemas for Foundry, derived from each product tool's input model.

The Phase 18 lesson, applied to a different transport: names, types and
required-ness come from the class that later validates them, so the schema
cannot drift from the contract it describes. Bounds are NOT emitted — a
`max_length` renders as `maxLength`, and schema keywords of that family are what
the Responses API rejected in Phase 18. The product's validators still enforce
them.

Risk is never emitted. It is not the model's business.
"""

from __future__ import annotations

import types
import typing
from typing import Any

_SIMPLE_TYPES: dict[object, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def function_schema_for(definition: Any) -> dict[str, Any]:
    """Build one strict function schema from a product `ToolDefinition`."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, field in definition.tool.input_model.model_fields.items():
        annotation = field.annotation
        origin = typing.get_origin(annotation)
        if origin is typing.Union or origin is types.UnionType:
            candidates = [a for a in typing.get_args(annotation) if a is not type(None)]
            annotation = candidates[0] if len(candidates) == 1 else None
        entry: dict[str, Any] = {"type": _SIMPLE_TYPES.get(annotation, "string")}
        if field.description:
            entry["description"] = field.description
        properties[name] = entry
        # Strict mode requires every declared property in `required`. Optional
        # arguments are expressed as nullable rather than omitted, because a
        # property missing from `required` is rejected outright.
        required.append(name)
        if not field.is_required():
            entry["type"] = [entry["type"], "null"]

    return {
        "type": "function",
        "name": definition.name,
        "description": definition.description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "strict": True,
    }


__all__ = ["function_schema_for"]
