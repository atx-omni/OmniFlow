from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigError
from .security import reject_secret_keys


DEFAULT_CONFIG_PATH = ".omniflow.yml"


def _expand_env_string(value: str) -> str:
    return re.sub(
        r"\$(\w+)|\$\{([^}]+)\}",
        lambda match: os.getenv(match.group(1) or match.group(2), ""),
        value,
    )


def expand_env_vars(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_env_string(value)
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    return value


def parse_bool(name: str, value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"Invalid boolean value for {name}: {value!r}")


def parse_csv(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ConfigError(f"Expected string list value, got {item!r}")
        for name in item.split(","):
            normalized = name.strip()
            if normalized and normalized not in names:
                names.append(normalized)
    return names


def config_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_raw_config(path: str | Path | None = None) -> tuple[dict[str, Any], Path | None]:
    candidates = [Path(path)] if path else [Path(DEFAULT_CONFIG_PATH)]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Could not parse config file '{candidate}': {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigError(f"Config file '{candidate}' must contain a top-level mapping")
        reject_secret_keys(payload, source=str(candidate))
        return expand_env_vars(payload), candidate
    return {}, None


@dataclass
class OmniSettings:
    base_url: str | None = None
    model_id: str | None = None
    branch_id: str | None = None
    branch_name: str | None = None
    user_id: str | None = None
    include_personal_folders: bool = False
    timeout: int = 60


@dataclass
class ContentValidationSettings:
    enabled: bool = True
    fail_on_new_only: bool = False
    labels: list[str] = field(default_factory=list)


@dataclass
class ModelValidationSettings:
    enabled: bool = True
    fail_on_warnings: bool = False


@dataclass
class SemanticLintSettings:
    enabled: bool = True
    rules: dict[str, str] = field(default_factory=dict)


@dataclass
class ContractSettings:
    enabled: bool = True
    fail_on_deleted_referenced_fields: bool = True
    fail_on_renamed_referenced_fields: bool = True
    fail_on_referenced_field_type_changes: bool = True
    fail_on_referenced_join_cardinality_changes: bool = True


@dataclass
class DbtExposureSettings:
    enabled: bool = False
    fail_on_unavailable: bool = False


@dataclass
class ReportingSettings:
    formats: list[str] = field(default_factory=lambda: ["json", "markdown"])
    output_dir: str = ".omniflow"


@dataclass
class SecuritySettings:
    redact_logs: bool = True
    allow_raw_response_output: bool = False
    max_report_samples: int = 20
    redact_document_names: bool = False
    redaction_level: str = "standard"
    retain_restricted_artifacts: bool = True


@dataclass
class OmniCIConfig:
    raw: dict[str, Any]
    source: Path | None
    omni: OmniSettings
    content_validation: ContentValidationSettings
    model_validation: ModelValidationSettings
    semantic_lint: SemanticLintSettings
    contracts: ContractSettings
    dbt_exposures: DbtExposureSettings
    reporting: ReportingSettings
    security: SecuritySettings
    hash: str


def load_config(path: str | Path | None = None) -> OmniCIConfig:
    raw, source = load_raw_config(path)
    return _to_config(raw, source)


def _to_config(raw: dict[str, Any], source: Path | None) -> OmniCIConfig:
    omni_raw = raw.get("omni", {}) or {}
    checks_raw = raw.get("checks", {}) or {}
    reporting_raw = raw.get("reporting", {}) or {}
    security_raw = raw.get("security", {}) or {}
    contracts_raw = raw.get("contracts", {}) or {}
    content_raw = checks_raw.get("content_validation", {}) or {}
    model_raw = checks_raw.get("model_validation", {}) or {}
    lint_raw = checks_raw.get("semantic_lint", {}) or {}
    exposures_raw = checks_raw.get("dbt_exposures", {}) or {}

    omni = OmniSettings(
        base_url=_string_env("OMNI_BASE_URL", omni_raw.get("base_url")),
        model_id=_string_env("OMNI_MODEL_ID", omni_raw.get("model_id")),
        branch_id=_string_env("OMNI_BRANCH_ID", omni_raw.get("branch_id")),
        branch_name=_string_env("OMNI_BRANCH_NAME", omni_raw.get("branch_name")),
        user_id=_string_env("OMNI_USER_ID", omni_raw.get("user_id")),
        include_personal_folders=parse_bool(
            "OMNI_INCLUDE_PERSONAL_FOLDERS",
            os.getenv("OMNI_INCLUDE_PERSONAL_FOLDERS", omni_raw.get("include_personal_folders")),
            False,
        ),
        timeout=int(os.getenv("OMNI_TIMEOUT", omni_raw.get("timeout", 60) or 60)),
    )
    content = ContentValidationSettings(
        enabled=parse_bool("content_validation.enabled", content_raw.get("enabled"), True),
        fail_on_new_only=parse_bool(
            "content_validation.fail_on_new_only",
            os.getenv("OMNI_FAIL_ON_NEW_ONLY", content_raw.get("fail_on_new_only")),
            False,
        ),
        labels=parse_csv(os.getenv("OMNI_LABELS", content_raw.get("labels"))),
    )
    model = ModelValidationSettings(
        enabled=parse_bool("model_validation.enabled", model_raw.get("enabled"), True),
        fail_on_warnings=parse_bool("model_validation.fail_on_warnings", model_raw.get("fail_on_warnings"), False),
    )
    lint = SemanticLintSettings(
        enabled=parse_bool("semantic_lint.enabled", lint_raw.get("enabled"), True),
        rules={str(key): str(value) for key, value in (lint_raw.get("rules") or {}).items()},
    )
    contracts = ContractSettings(
        enabled=parse_bool("contracts.enabled", contracts_raw.get("enabled"), True),
        fail_on_deleted_referenced_fields=parse_bool(
            "contracts.fail_on.deleted_referenced_fields",
            (contracts_raw.get("fail_on") or {}).get("deleted_referenced_fields")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
        fail_on_renamed_referenced_fields=parse_bool(
            "contracts.fail_on.renamed_referenced_fields",
            (contracts_raw.get("fail_on") or {}).get("renamed_referenced_fields")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
        fail_on_referenced_field_type_changes=parse_bool(
            "contracts.fail_on.referenced_field_type_changes",
            (contracts_raw.get("fail_on") or {}).get("referenced_field_type_changes")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
        fail_on_referenced_join_cardinality_changes=parse_bool(
            "contracts.fail_on.referenced_join_cardinality_changes",
            (contracts_raw.get("fail_on") or {}).get("referenced_join_cardinality_changes")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
    )
    dbt_exposures = DbtExposureSettings(
        enabled=parse_bool("dbt_exposures.enabled", exposures_raw.get("enabled"), False),
        fail_on_unavailable=parse_bool(
            "dbt_exposures.fail_on_unavailable",
            exposures_raw.get("fail_on_unavailable"),
            False,
        ),
    )
    reporting = ReportingSettings(
        formats=parse_csv(reporting_raw.get("formats")) or ["json", "markdown"],
        output_dir=str(reporting_raw.get("output_dir") or ".omniflow"),
    )
    security = SecuritySettings(
        redact_logs=parse_bool("security.redact_logs", security_raw.get("redact_logs"), True),
        allow_raw_response_output=parse_bool(
            "security.allow_raw_response_output",
            security_raw.get("allow_raw_response_output"),
            False,
        ),
        max_report_samples=int(security_raw.get("max_report_samples", 20) or 20),
        redact_document_names=parse_bool("security.redact_document_names", security_raw.get("redact_document_names"), False),
        redaction_level=_redaction_level(security_raw.get("redaction_level", "standard")),
        retain_restricted_artifacts=parse_bool(
            "security.retain_restricted_artifacts",
            security_raw.get("retain_restricted_artifacts"),
            True,
        ),
    )
    return OmniCIConfig(
        raw=raw,
        source=source,
        omni=omni,
        content_validation=content,
        model_validation=model,
        semantic_lint=lint,
        contracts=contracts,
        dbt_exposures=dbt_exposures,
        reporting=reporting,
        security=security,
        hash=config_hash(raw),
    )


def _string_env(env_name: str, configured: Any) -> str | None:
    value = os.getenv(env_name, configured)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Expected {env_name} to resolve to a string")
    stripped = value.strip()
    return stripped or None


def _redaction_level(value: Any) -> str:
    if value is None:
        return "standard"
    if not isinstance(value, str):
        raise ConfigError("security.redaction_level must be 'standard' or 'strict'")
    normalized = value.strip().lower()
    if normalized not in {"standard", "strict"}:
        raise ConfigError("security.redaction_level must be 'standard' or 'strict'")
    return normalized


def require_api_key() -> str:
    value = os.getenv("OMNI_API_KEY")
    if not value:
        raise ConfigError("Missing OMNI_API_KEY. API keys are only read from environment variables.")
    return value
