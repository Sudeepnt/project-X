"""Offline drafts: deterministic output with no network.

Modules register a generator per task (``@offline_generator("gtm.content.write_post")``) that
builds a sensible answer from the request's structured ``context``. Tasks without one get a
schema-valid instance built from field names. Everything the offline provider writes is a
draft meant for review; it lets the product run end to end before any model key exists.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import orjson

from projectx.spine.gateway.contracts import ModelRequest

OfflineGenerator = Callable[[ModelRequest], "str | dict[str, Any] | list[Any]"]
_GENERATORS: dict[str, OfflineGenerator] = {}


def offline_generator(task: str) -> Callable[[OfflineGenerator], OfflineGenerator]:
    def decorator(fn: OfflineGenerator) -> OfflineGenerator:
        _GENERATORS[task] = fn
        return fn

    return decorator


def generate_offline(req: ModelRequest) -> str:
    generator = _GENERATORS.get(req.task)
    if generator is not None:
        out = generator(req)
    elif req.output_schema is not None:
        out = fake_from_schema(req.output_schema.model_json_schema(), req.context, seed=req.task)
    else:
        topic = req.context.get("topic") or (req.messages[-1].content[:160] if req.messages else "")
        out = f"Draft: {topic}".strip()
    if isinstance(out, str):
        return out
    return orjson.dumps(out).decode()


def stable_pick[T](options: list[T], *keys: object) -> T:
    """Pick deterministically from options, so the same inputs give the same draft."""
    digest = hashlib.sha256("|".join(str(k) for k in keys).encode()).digest()
    return options[int.from_bytes(digest[:4], "big") % len(options)]


def fake_from_schema(schema: dict[str, Any], context: dict[str, Any], *, seed: str = "") -> Any:
    defs = schema.get("$defs", {})

    def build(node: dict[str, Any], name: str, depth: int) -> Any:
        if "$ref" in node:
            return build(defs[node["$ref"].split("/")[-1]], name, depth)
        if "anyOf" in node:
            options = [n for n in node["anyOf"] if n.get("type") != "null"]
            return build(options[0], name, depth) if options else None
        if "enum" in node:
            return node["enum"][0]
        if "const" in node:
            return node["const"]
        kind = node.get("type")
        if kind == "object" or "properties" in node:
            return {k: build(v, k, depth + 1) for k, v in node.get("properties", {}).items()}
        if kind == "array":
            count = max(node.get("minItems", 0), 2 if depth < 3 else 1)
            return [build(node.get("items", {}), name, depth + 1) for _ in range(count)]
        if kind == "integer":
            return max(int(node.get("minimum", 1)), 1)
        if kind == "number":
            low = float(node.get("minimum", 0.0))
            high = float(node.get("maximum", 1.0))
            return round((low + high) / 2, 2) if high > low else low
        if kind == "boolean":
            return False
        value = context.get(name)
        if isinstance(value, str) and value:
            text = value
        else:
            subject = context.get("business_name") or context.get("topic") or "the business"
            text = f"{name.replace('_', ' ').capitalize()} for {subject}"
        max_len = node.get("maxLength")
        return text[:max_len] if max_len else text

    return build(schema, "root", 0)
