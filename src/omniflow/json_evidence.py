"""Strict object decoding for evidence, without disclosing response content."""

from __future__ import annotations

from typing import Any


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Do not silently overwrite evidence with a later JSON object member."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON evidence contains duplicate object members")
        result[key] = value
    return result
