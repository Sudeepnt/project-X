"""Turn a Pydantic model into a JSON schema that structured-output engines accept.

Structured-output modes support a subset of JSON Schema: every object needs
``additionalProperties: false`` and a full ``required`` list, and numeric/length constraints are
not enforced. We strip what is unsupported; Pydantic re-validates the answer with the full
model afterwards, so the constraints still hold.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_DROP = frozenset(
    {
        "title",
        "default",
        "examples",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "format",
        "minProperties",
        "maxProperties",
    }
)


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP:
            continue
        if key in ("properties", "$defs"):
            out[key] = {k: _clean(v) for k, v in value.items()}
        else:
            out[key] = _clean(value)
    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties", {})
        out["additionalProperties"] = False
        out["required"] = list(props.keys())
    return out


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    cleaned: dict[str, Any] = _clean(model.model_json_schema())
    return cleaned
