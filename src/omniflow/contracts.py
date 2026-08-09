from __future__ import annotations

import datetime as dt
from typing import Any

from .config import ContractSettings


BREAKING_RULES = {
    "field_deleted": "deleted_referenced_fields",
    "field_renamed": "renamed_referenced_fields",
    "field_type_changed": "referenced_field_type_changes",
    "relationship_cardinality_changed": "referenced_join_cardinality_changes",
}


def evaluate_contracts(
    *,
    diff_result: dict[str, Any],
    dependencies: dict[str, Any],
    settings: ContractSettings,
    model_id: str,
) -> tuple[dict[str, Any], int]:
    references = _index_references(dependencies)
    impacts = []
    for change in diff_result.get("changes", []):
        referenced_content = _content_for_change(change, references)
        impact_level = _impact_level(change, referenced_content)
        should_fail = settings.enabled and _should_fail(change, impact_level, settings)
        impacts.append(
            {
                **change,
                "validator": "contracts",
                "impact_level": impact_level,
                "referenced": bool(referenced_content),
                "referenced_content": referenced_content,
                "severity": "error" if should_fail else ("warning" if impact_level != "unreferenced" else "info"),
            }
        )
    failing = [impact for impact in impacts if impact.get("severity") == "error"]
    report = {
        "tool": "omniflow",
        "validator": "contracts",
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "model_id": model_id,
        "summary": {
            "total_impacts": len(impacts),
            "referenced_breaking": len(failing),
            "referenced": sum(1 for impact in impacts if impact["referenced"]),
            "unreferenced": sum(1 for impact in impacts if not impact["referenced"]),
            "dependency_generation_mode": dependencies.get("generation_mode"),
            "dependency_coverage_gaps": len(dependencies.get("coverage_gaps", [])) if isinstance(dependencies.get("coverage_gaps"), list) else 0,
        },
        "dependency_generation_mode": dependencies.get("generation_mode"),
        "coverage_gaps": dependencies.get("coverage_gaps", []) if isinstance(dependencies.get("coverage_gaps"), list) else [],
        "issues": impacts,
    }
    return report, 1 if failing else 0


def _index_references(dependencies: dict[str, Any]) -> dict[tuple[str | None, str], list[dict[str, Any]]]:
    indexed: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for item in dependencies.get("dependencies", []):
        if not isinstance(item, dict):
            continue
        content = {
            "content_id": item.get("content_id"),
            "content_type": item.get("content_type"),
            "content_name": item.get("content_name"),
            "owner": item.get("owner"),
            "labels": item.get("labels") if isinstance(item.get("labels"), list) else [],
            "query_id": item.get("query_id"),
            "query_name": item.get("query_name"),
        }
        for ref in item.get("references", []):
            if not isinstance(ref, dict) or not isinstance(ref.get("name"), str):
                continue
            ref_type = ref.get("type") if isinstance(ref.get("type"), str) else None
            indexed.setdefault((ref_type, ref["name"]), []).append(content)
    return indexed


def _content_for_change(change: dict[str, Any], references: dict[tuple[str | None, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    names = []
    for key in ("field", "previous_field", "name", "previous_name"):
        value = change.get(key)
        if isinstance(value, str) and value:
            names.append(value)
    ref_types = _reference_types_for_change(change)
    deduped: list[dict[str, Any]] = []
    seen = set()
    for name in names:
        for ref_type in ref_types:
            for content in references.get((ref_type, name), []):
                identity = (content.get("content_id"), content.get("query_id"), ref_type, name)
                if identity not in seen:
                    seen.add(identity)
                    deduped.append(content)
    return deduped


def _reference_types_for_change(change: dict[str, Any]) -> tuple[str | None, ...]:
    change_type = str(change.get("type") or "")
    if change.get("field") or change.get("previous_field") or change_type.startswith("field_") or change_type.startswith("measure_"):
        return ("field",)
    if change_type.startswith("view_"):
        return ("view",)
    if change_type.startswith("topic_"):
        return ("topic",)
    if change_type.startswith("relationship_"):
        return ("relationship", "field")
    return (None, "field", "view", "topic", "relationship")


def _impact_level(change: dict[str, Any], referenced_content: list[dict[str, Any]]) -> str:
    if not referenced_content:
        return "unreferenced"
    if change.get("type") in BREAKING_RULES:
        return "referenced_breaking"
    if change.get("risk") in {"breaking", "security_sensitive", "governance_sensitive"}:
        return "referenced_warning"
    return "referenced_safe"


def _should_fail(change: dict[str, Any], impact_level: str, settings: ContractSettings) -> bool:
    if impact_level != "referenced_breaking":
        return False
    rule = BREAKING_RULES.get(str(change.get("type")))
    if rule == "deleted_referenced_fields":
        return settings.fail_on_deleted_referenced_fields
    if rule == "renamed_referenced_fields":
        return settings.fail_on_renamed_referenced_fields
    if rule == "referenced_field_type_changes":
        return settings.fail_on_referenced_field_type_changes
    if rule == "referenced_join_cardinality_changes":
        return settings.fail_on_referenced_join_cardinality_changes
    return False
