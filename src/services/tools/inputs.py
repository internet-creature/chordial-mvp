"""the blank-argument seam for chordial's tool handlers.

a model-facing handler reads absence as `None` (`tool_input.get(key) is
None` means "leave it alone"). some models don't leave a field alone: they
pad every optional argument they aren't setting with a placeholder - "" for
strings, most visibly - and a handler that applies "" as a real value does
damage (a blank plan name matched every plan; a blank next_action cleared a
scope). the dainframe's openai provider now renders tools strictly so the
model says null instead, and strips the nulls; this module is the
product-side defense for any path that still arrives padded: a blank or
whitespace-only string is not a value, it's an omission.

clearing a field is therefore always an explicit argument (update_task's
`clear_next_action`), never an empty string.
"""
from __future__ import annotations

from typing import Mapping


def is_blank(value) -> bool:
    """True for None and for strings with nothing in them."""
    return value is None or (isinstance(value, str) and not value.strip())


def without_blanks(tool_input: Mapping[str, object]) -> dict:
    """the same arguments minus every top-level key whose value is blank."""
    return {k: v for k, v in tool_input.items() if not is_blank(v)}


def blank_tolerant(handler):
    """wrap a handler so it never sees a blank top-level argument."""
    async def _run(tool_input, context):
        return await handler(without_blanks(tool_input or {}), context)
    _run.__name__ = getattr(handler, "__name__", "handler")
    _run.__wrapped__ = handler
    return _run
