"""Walk a Pydantic JSON schema and emit a minimal example payload.

Placeholders chosen for readability when an engineer opens ``default.json``
and edits it by hand:
  * strings → empty string (plus hints for known ``format``s)
  * ints / floats → 0
  * bools → false
  * arrays → empty list
  * objects → recurse into ``properties``

Not a full JSON Schema evaluator — good enough for generating diffable
starter inputs that an engineer refines by hand.
"""

from __future__ import annotations

from typing import Any


def example_from_schema(schema: dict[str, Any]) -> Any:
    return _walk(schema, schema.get("$defs") or schema.get("definitions") or {})


def _walk(node: Any, defs: dict[str, Any]) -> Any:
    if not isinstance(node, dict):
        return None
    if "$ref" in node:
        ref = node["$ref"]
        key = ref.rsplit("/", 1)[-1]
        target = defs.get(key)
        if target is None:
            return None
        return _walk(target, defs)
    if "enum" in node and node["enum"]:
        return node["enum"][0]
    if "const" in node:
        return node["const"]
    if "default" in node:
        return node["default"]
    if "anyOf" in node or "oneOf" in node:
        branches = node.get("anyOf") or node.get("oneOf") or []
        # Prefer the first non-null branch.
        for branch in branches:
            if not (isinstance(branch, dict) and branch.get("type") == "null"):
                return _walk(branch, defs)
        return None

    type_ = node.get("type")
    if type_ == "object":
        out: dict[str, Any] = {}
        props = node.get("properties", {})
        # Emit every property — makes the sample self-documenting. Optional vs.
        # required isn't encoded in the output; the developer editing the
        # sample can inspect the schema panel in the Runner UI.
        for key, sub in props.items():
            out[key] = _walk(sub, defs)
        return out
    if type_ == "array":
        items = node.get("items", {})
        if items:
            return [_walk(items, defs)]
        return []
    if type_ in ("integer", "number"):
        return 0
    if type_ == "boolean":
        return False
    if type_ == "null":
        return None
    if type_ == "string":
        fmt = node.get("format")
        if fmt == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        if fmt == "date-time":
            return "2024-01-01T00:00:00Z"
        if fmt == "date":
            return "2024-01-01"
        if fmt == "email":
            return "user@example.com"
        if fmt == "uri" or fmt == "url":
            return "https://example.com"
        return ""
    return None
