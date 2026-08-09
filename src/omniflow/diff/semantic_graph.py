from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SemanticGraph:
    views: dict[str, dict[str, Any]] = field(default_factory=dict)
    topics: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: dict[str, dict[str, Any]] = field(default_factory=dict)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)


def build_graph(files: dict[str, Any]) -> SemanticGraph:
    graph = SemanticGraph()
    for file_path, payload in files.items():
        if not isinstance(payload, dict):
            continue
        kind = _infer_kind(file_path, payload)
        name = _name(file_path, payload)
        if kind == "topic":
            graph.topics[name] = {"file": file_path, **payload}
        elif kind == "relationship":
            _add_relationships(graph, file_path, payload)
        else:
            graph.views[name] = {"file": file_path, **payload}
            _add_fields(graph, file_path, name, payload)
            _add_relationships(graph, file_path, payload)
    return graph


def _infer_kind(file_path: str, payload: dict[str, Any]) -> str:
    lower = file_path.lower()
    if lower.endswith(".topic") or ".topic." in lower:
        return "topic"
    if lower.endswith(".composite_topic") or ".composite_topic." in lower:
        return "topic"
    if "relationship" in lower:
        return "relationship"
    if payload.get("type") == "topic" or "base_view" in payload:
        return "topic"
    return "view"


def _name(file_path: str, payload: dict[str, Any]) -> str:
    value = payload.get("name") or payload.get("view") or payload.get("topic")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return file_path.rsplit("/", 1)[-1].split(".", 1)[0]


def _iter_field_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    groups = []
    for key in ("fields", "dimensions", "measures"):
        value = payload.get(key)
        if isinstance(value, dict):
            groups.append(value)
        elif isinstance(value, list):
            groups.append({str(item.get("name")): item for item in value if isinstance(item, dict) and item.get("name")})
    return groups


def _add_fields(graph: SemanticGraph, file_path: str, view_name: str, payload: dict[str, Any]) -> None:
    for group in _iter_field_groups(payload):
        for field_name, field in group.items():
            if not isinstance(field, dict):
                continue
            key = f"{view_name}.{field_name}"
            graph.fields[key] = {"file": file_path, "view": view_name, "name": field_name, **field}


def _add_relationships(graph: SemanticGraph, file_path: str, payload: dict[str, Any]) -> None:
    relationships = payload.get("relationships") or payload.get("joins")
    if isinstance(relationships, dict):
        items = relationships.items()
    elif isinstance(relationships, list):
        items = [(item.get("name") or item.get("join_to") or str(index), item) for index, item in enumerate(relationships) if isinstance(item, dict)]
    else:
        return
    for name, relationship in items:
        if isinstance(relationship, dict):
            graph.relationships[str(name)] = {"file": file_path, "name": str(name), **relationship}
