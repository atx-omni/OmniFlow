from __future__ import annotations

from .exceptions import ConfigError
from .security import validate_path_segment

EDITABLE_YAML_FILES = {"model", "relationships"}
EDITABLE_YAML_SUFFIXES = (".topic", ".composite_topic", ".view")


def validate_editable_yaml_file_name(value: str, *, allow_special_files: bool = True) -> str:
    if not isinstance(value, str):
        raise ConfigError("YAML file name must be a string")
    normalized = validate_path_segment(value.strip(), name="file_name")
    if (allow_special_files and normalized in EDITABLE_YAML_FILES) or normalized.endswith(EDITABLE_YAML_SUFFIXES):
        return normalized
    expected = "model, relationships, .topic, .composite_topic, or .view"
    raise ConfigError(f"YAML file name must be one of the documented editable file types: {expected}")
