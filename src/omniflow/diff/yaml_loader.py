from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..exceptions import ConfigError


def load_yaml_files(root: str | Path) -> dict[str, Any]:
    base = Path(root)
    files: dict[str, Any] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or not _is_yaml_model_file(path):
            continue
        rel = path.relative_to(base).as_posix()
        text = path.read_text(encoding="utf-8")
        files[rel] = _parse_yaml(text, source=rel)
    return files


def parse_yaml_file_map(files: dict[str, str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for name, text in files.items():
        if isinstance(text, str):
            parsed[name] = _parse_yaml(text, source=name)
    return parsed


def _is_yaml_model_file(path: Path) -> bool:
    return path.name in {"model", "relationships"} or path.suffix in {
        ".yaml",
        ".yml",
        ".view",
        ".topic",
        ".composite_topic",
    }


def _parse_yaml(text: str, *, source: str) -> Any:
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse Omni YAML file '{source}': {exc}") from exc
