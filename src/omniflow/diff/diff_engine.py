from __future__ import annotations

from typing import Any

from .semantic_graph import SemanticGraph


def diff_graphs(base: SemanticGraph, head: SemanticGraph) -> dict[str, Any]:
    changes = []
    changes.extend(_diff_named("view", base.views, head.views))
    changes.extend(_diff_named("topic", base.topics, head.topics))
    changes.extend(_diff_named("relationship", base.relationships, head.relationships))
    changes.extend(_diff_named("field", base.fields, head.fields))
    changes.extend(_field_renames(base.fields, head.fields))
    changes.extend(_field_property_changes(base.fields, head.fields))
    changes.extend(_relationship_property_changes(base.relationships, head.relationships))
    risk_level = _max_risk(changes)
    return {"risk_level": risk_level, "changes": changes}


def _diff_named(kind: str, base_items: dict[str, Any], head_items: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(head_items) - set(base_items)):
        changes.append(_change(f"{kind}_added", head_items[name], name, "info", f"Added {kind} {name}."))
    for name in sorted(set(base_items) - set(head_items)):
        risk = "breaking" if kind in {"field", "view", "topic"} else "warning"
        changes.append(_change(f"{kind}_deleted", base_items[name], name, risk, f"Deleted {kind} may break content references."))
    for name in sorted(set(base_items) & set(head_items)):
        if _normalized(base_items[name]) != _normalized(head_items[name]):
            changes.append(_change(f"{kind}_modified", head_items[name], name, "warning", f"Modified {kind} {name}."))
    return changes


def _field_property_changes(base_fields: dict[str, Any], head_fields: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(base_fields) & set(head_fields)):
        base = base_fields[name]
        head = head_fields[name]
        if base.get("type") != head.get("type"):
            changes.append(_change("field_type_changed", head, name, "breaking", "Field type changed."))
        if base.get("aggregate_type") != head.get("aggregate_type"):
            changes.append(_change("measure_aggregation_changed", head, name, "warning", "Measure aggregation changed."))
        for key in ("access_grants", "access_filters", "required_access_grants"):
            if base.get(key) != head.get(key):
                changes.append(_change("governance_changed", head, name, "governance_sensitive", f"{key} changed."))
    return changes


def _field_renames(base_fields: dict[str, Any], head_fields: dict[str, Any]) -> list[dict[str, Any]]:
    deleted = {name: field for name, field in base_fields.items() if name not in head_fields}
    added = {name: field for name, field in head_fields.items() if name not in base_fields}
    changes = []
    for previous_name, previous in deleted.items():
        for next_name, current in added.items():
            if previous.get("file") != current.get("file"):
                continue
            if _normalized_field_for_rename(previous) != _normalized_field_for_rename(current):
                continue
            changes.append(
                {
                    "type": "field_renamed",
                    "file": current.get("file"),
                    "risk": "breaking",
                    "message": "Renamed field may break content references.",
                    "previous_field": previous_name,
                    "field": next_name,
                }
            )
    return changes


def _relationship_property_changes(base_relationships: dict[str, Any], head_relationships: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(base_relationships) & set(head_relationships)):
        base = base_relationships[name]
        head = head_relationships[name]
        for key in ("relationship", "relationship_type", "cardinality", "type"):
            if base.get(key) != head.get(key):
                changes.append(_change("relationship_cardinality_changed", head, name, "warning", "Join relationship/cardinality changed."))
                break
    return changes


def _change(change_type: str, item: dict[str, Any], name: str, risk: str, message: str) -> dict[str, Any]:
    payload = {"type": change_type, "file": item.get("file"), "risk": risk, "message": message}
    if "." in name:
        payload["field"] = name
    else:
        payload["name"] = name
    return payload


def _normalized(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "file"}


def _normalized_field_for_rename(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in {"file", "name"}
    }


def _max_risk(changes: list[dict[str, Any]]) -> str:
    order = ["info", "warning", "governance_sensitive", "security_sensitive", "breaking"]
    if not changes:
        return "info"
    return max((change.get("risk", "info") for change in changes), key=lambda risk: order.index(risk) if risk in order else 0)
