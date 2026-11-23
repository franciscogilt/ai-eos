"""JSON Schema preparation for tool parameters.

Vendors differ in how much JSON Schema they accept; local `$ref`s are the most common gap.
`inline_refs` replaces every `{"$ref": "#/$defs/X"}` with the definition itself so each
vendor receives one self-contained schema. Recursive models are refused rather than
expanded forever.
"""

from __future__ import annotations

import copy
from typing import Any

_DEF_KEYS = ("$defs", "definitions")


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    root = copy.deepcopy(schema)
    defs: dict[str, Any] = {}
    for key in _DEF_KEYS:
        defs.update({f"#/{key}/{name}": d for name, d in (root.pop(key, None) or {}).items()})

    def walk(node: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref not in defs:
                    raise ValueError(f"unsupported $ref {ref!r}")
                if ref in stack:
                    raise ValueError(f"recursive schema through {ref!r} is not supported")
                siblings = {k: v for k, v in node.items() if k != "$ref"}
                resolved = walk(copy.deepcopy(defs[ref]), (*stack, ref))
                return {**resolved, **{k: walk(v, stack) for k, v in siblings.items()}}
            return {k: walk(v, stack) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, stack) for v in node]
        return node

    result = walk(root, ())
    assert isinstance(result, dict)
    return result
