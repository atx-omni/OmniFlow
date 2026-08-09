from __future__ import annotations

import datetime as dt
from typing import Any

from .config import DbtExposureSettings
from .exceptions import OmniCIError
from .omni_client import OmniClient
from .security import redact


def run_dbt_exposure_enrichment(
    *,
    client: OmniClient,
    model_id: str,
    settings: DbtExposureSettings,
) -> tuple[dict[str, Any], int]:
    try:
        payload = client.get_dbt_exposures(model_id)
        exposures = _normalize_exposures(payload)
        report = {
            "tool": "omniflow",
            "validator": "dbt_exposures",
            "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "model_id": model_id,
            "summary": {
                "total_exposures": len(exposures),
                "coverage_status": "available",
            },
            "exposures": exposures,
            "issues": [],
        }
        return report, 0
    except OmniCIError as exc:
        issue = {
            "validator": "dbt_exposures",
            "severity": "error" if settings.fail_on_unavailable else "warning",
            "message": f"dbt exposure enrichment unavailable: {redact(str(exc))}",
        }
        report = {
            "tool": "omniflow",
            "validator": "dbt_exposures",
            "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "model_id": model_id,
            "summary": {
                "total_exposures": 0,
                "coverage_status": "unavailable",
            },
            "coverage_gaps": [
                {
                    "type": "dbt_exposures",
                    "name": model_id,
                    "message": issue["message"],
                }
            ],
            "issues": [issue],
        }
        return report, 1 if settings.fail_on_unavailable else 0


def _normalize_exposures(payload: Any) -> list[dict[str, Any]]:
    records = _exposure_records(payload)
    exposures = []
    for record in records:
        if not isinstance(record, dict):
            continue
        exposures.append(
            {
                "id": record.get("id") or record.get("content_id") or record.get("dashboard_id"),
                "name": record.get("name") or record.get("dashboard_name") or record.get("content_name"),
                "type": record.get("type") or record.get("content_type") or "dashboard",
                "url": record.get("url") or record.get("content_url"),
                "owner": record.get("owner"),
                "depends_on": _depends_on(record),
                "maturity": record.get("maturity"),
            }
        )
    return exposures


def _exposure_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("exposures", "records", "items", "dashboards", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _depends_on(record: dict[str, Any]) -> list[str]:
    value = record.get("depends_on") or record.get("dependsOn") or record.get("dependencies")
    if not isinstance(value, list):
        return []
    depends_on = []
    for item in value:
        if isinstance(item, str):
            depends_on.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("unique_id") or item.get("id")
            if isinstance(name, str):
                depends_on.append(name)
    return depends_on
