from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .reporting.writer import write_reports
from .security import public_safe


PUBLIC_DIR = "public"
RESTRICTED_DIR = "restricted"


def public_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / PUBLIC_DIR


def restricted_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / RESTRICTED_DIR


def write_public_reports(
    report: dict[str, Any],
    *,
    output_dir: str | Path,
    formats: list[str],
    redaction_level: str,
) -> dict[str, Any]:
    safe_report = public_safe(report, redaction_level=redaction_level)
    write_reports(safe_report, output_dir=output_dir, formats=formats)
    write_reports(safe_report, output_dir=public_dir(output_dir), formats=formats)
    return safe_report


def write_public_json(
    path: str | Path,
    payload: dict[str, Any],
    *,
    redaction_level: str,
) -> dict[str, Any]:
    safe_payload = public_safe(payload, redaction_level=redaction_level)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(safe_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return safe_payload


def write_artifact_manifest(
    *,
    output_dir: str | Path,
    restricted_artifacts_enabled: bool,
    redaction_level: str,
) -> None:
    root = Path(output_dir)
    manifest = {
        "version": 1,
        "public_dir": PUBLIC_DIR,
        "restricted_dir": RESTRICTED_DIR,
        "restricted_artifacts_enabled": restricted_artifacts_enabled,
        "redaction_level": redaction_level,
        "public_artifacts": [
            f"{PUBLIC_DIR}/report.json",
            f"{PUBLIC_DIR}/report.md",
            f"{PUBLIC_DIR}/report.sarif",
            f"{PUBLIC_DIR}/junit.xml",
            f"{PUBLIC_DIR}/evidence.json",
        ],
        "restricted_artifacts": [
            f"{RESTRICTED_DIR}/<model_id>/yaml-base/",
            f"{RESTRICTED_DIR}/<model_id>/yaml-head/",
            f"{RESTRICTED_DIR}/<model_id>/dependencies.json",
            f"{RESTRICTED_DIR}/<model_id>/semantic-diff.json",
            f"{RESTRICTED_DIR}/<model_id>/contract-impact.json",
            f"{RESTRICTED_DIR}/<model_id>/content-report.json",
        ],
    }
    (root / "artifact-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
