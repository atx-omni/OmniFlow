from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .omni_client import OmniClient

SUPPORTED_YAML_MODES = {"extension", "staged", "combined"}


def pull_yaml(
    *,
    client: OmniClient,
    model_id: str,
    branch_id: str | None,
    output_dir: str | Path,
    mode: str = "combined",
    include_checksums: bool = True,
    fully_resolved: bool = False,
) -> dict[str, Any]:
    if mode not in SUPPORTED_YAML_MODES:
        raise ConfigError(f"Unsupported YAML pull mode '{mode}'. Expected one of: combined, extension, staged.")
    if branch_id and mode != "combined":
        raise ConfigError("Omni YAML branch pulls require mode='combined' according to the Omni API contract.")
    payload = client.get_model_yaml(
        model_id,
        branch_id=branch_id,
        mode=mode,
        include_checksums=include_checksums,
        fully_resolved=fully_resolved,
    )
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    files = _extract_file_map(payload)
    checksums = _extract_checksums(payload)
    manifest_files: dict[str, dict[str, str | None]] = {}
    for file_name, text in files.items():
        target = root / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        manifest_files[file_name] = {
            "checksum": checksums.get(file_name),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    manifest = {
        "model_id": model_id,
        "branch_id": branch_id,
        "mode": mode,
        "fully_resolved": fully_resolved,
        "files": manifest_files,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _extract_file_map(payload: dict[str, Any]) -> dict[str, str]:
    candidates = payload.get("files") or payload.get("fileMap") or payload.get("yaml")
    files: dict[str, str] = {}
    if isinstance(candidates, dict):
        for key, value in candidates.items():
            if isinstance(value, str):
                files[key] = value
            elif isinstance(value, dict) and isinstance(value.get("contents"), str):
                files[key] = value["contents"]
            elif isinstance(value, dict) and isinstance(value.get("content"), str):
                files[key] = value["content"]
    return files


def _extract_checksums(payload: dict[str, Any]) -> dict[str, str]:
    checksums = payload.get("checksums")
    if isinstance(checksums, dict):
        return {str(key): str(value) for key, value in checksums.items()}
    files = payload.get("files") or payload.get("fileMap")
    if isinstance(files, dict):
        return {
            str(key): str(value.get("checksum"))
            for key, value in files.items()
            if isinstance(value, dict) and value.get("checksum") is not None
        }
    return {}
