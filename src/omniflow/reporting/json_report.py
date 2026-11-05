from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..security import redact, secure_write_text

MAX_PUBLIC_REPORT_BYTES = 16 * 1024 * 1024
RECOVERY_FIELDS = frozenset({
    "outcome_label", "recovery_steps", "verify_recovery", "support_action", "guide_anchor", "coverage_description",
})


def diagnostic_json(payload: dict[str, Any]) -> str:
    """Keep all occurrences and legacy evidence when repeated guidance grows a public artifact."""
    pretty = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    # json.dumps defaults to ASCII, so this length is the UTF-8 byte length.
    if (len(pretty) <= MAX_PUBLIC_REPORT_BYTES or payload.get("diagnostic_schema_version") != 2
            or payload.get("diagnostic_audience") == "repository"):
        return pretty
    del pretty
    catalog: dict[str, dict] = {}
    references: dict[str, str] = {}

    def compact(value: Any, include_references: bool = True) -> Any:
        if isinstance(value, list):
            return [compact(item, include_references) for item in value]
        if not isinstance(value, dict):
            return value
        fields = {key: value[key] for key in RECOVERY_FIELDS if key in value} if "diagnostic_code" in value else {}
        result = {key: compact(item, include_references) for key, item in value.items() if key not in fields}
        if fields and include_references:
            signature = json.dumps(fields, sort_keys=True, separators=(",", ":"))
            reference = references.get(signature)
            if reference is None:
                reference = f"r{len(catalog) + 1}"
                references[signature] = reference
                catalog[reference] = fields
            result["recovery_ref"] = reference
        return result

    compacted = compact(payload)
    if catalog:
        compacted["recovery_catalog"] = catalog
    encoded = json.dumps(compacted, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded) > MAX_PUBLIC_REPORT_BYTES and catalog:
        # Preserve a previously publishable report even when its old fields
        # alone nearly fill the reader bound. Presenters rebuild guidance from
        # reason, state and scope, never from prose in a downloaded catalog.
        return json.dumps(compact(payload, False), sort_keys=True, separators=(",", ":")) + "\n"
    return encoded


def write_json_report(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    secure_write_text(target, diagnostic_json(redact(payload)))
