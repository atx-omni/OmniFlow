from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_yaml_files(root: str | Path) -> dict[str, Any]:
    base = Path(root)
    files: dict[str, Any] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".yml", ".view", ".topic", ".composite_topic"}:
            continue
        rel = path.relative_to(base).as_posix()
        text = path.read_text(encoding="utf-8")
        files[rel] = yaml.safe_load(text) or {}
    return files


def parse_yaml_file_map(files: dict[str, str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for name, text in files.items():
        if isinstance(text, str):
            parsed[name] = yaml.safe_load(text) or {}
    return parsed
