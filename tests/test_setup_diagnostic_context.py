import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omniflow.config import (
    CONFIG_DIAGNOSTIC_KEYS,
    load_config,
    require_api_key,
    require_repair_api_key,
    require_sync_api_key,
)
from omniflow.discovery import get_changed_files, load_flow_metadata, reset_changed_files_cache
from omniflow.exceptions import ConfigError, SecurityPolicyError, exception_diagnostic
from omniflow.trust import read_trusted_repo_text
from omniflow.yaml_security import parse_secure_yaml


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("GITHUB_", "OMNI_", "OMNIFLOW_")):
            monkeypatch.delenv(name)
    reset_changed_files_cache()


@pytest.mark.parametrize("body,key,expected,reason", [
    ("checks:\n  model_validation:\n    enabled: [sensitive-value]\n",
     "checks.model_validation.enabled", "boolean", "config_invalid_type"),
    ("omni:\n  timeout: sensitive-value\n", "omni.timeout", "integer", "config_invalid_type"),
    ("checks:\n  content_validation: sensitive-value\n",
     "checks.content_validation", "mapping", "config_invalid_type"),
    ("checks:\n  model_validation:\n    sensitive-key: sensitive-value\n",
     "checks.model_validation", "mapping", "config_unknown_key"),
    ("validation:\n  mode: sensitive-value\n", "validation.mode", "enum", "config_invalid_value"),
    ("reporting:\n  output_dir: [sensitive-value]\n",
     "reporting.output_dir", "repository_path", "config_invalid_type"),
])
def test_known_policy_key_and_expected_type_without_rejected_values(tmp_path, body, key, expected, reason):
    path = tmp_path / "private-policy.yml"
    path.write_text(body)
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    evidence = exception_diagnostic(caught.value)
    assert evidence == {
        "reason_code": reason, "source_kind": "policy", "config_key": key, "expected_type": expected,
    }
    assert key in CONFIG_DIAGNOSTIC_KEYS
    assert "sensitive" not in json.dumps(evidence)
    assert "sensitive" not in str(caught.value)


def test_protected_source_exclusion_has_typed_config_location(tmp_path):
    path = tmp_path / ".omniflow.yml"
    path.write_text("files:\n  non_omni:\n    - tag: docs\n      pattern: omni/orders.view.yaml\n"
                    "      reason: sensitive-value\n")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert exception_diagnostic(caught.value) == {
        "reason_code": "non_omni_source_conflict", "source_kind": "policy",
        "config_key": "files.non_omni", "expected_type": "list", "entry_index": 1,
    }


@pytest.mark.parametrize("source_kind", ["policy", "model_yaml"])
def test_yaml_parser_location_does_not_expose_source_or_scalar(source_kind):
    with pytest.raises(ConfigError) as caught:
        parse_secure_yaml("private_value: [sensitive-value\n", source="private-source-name", source_kind=source_kind)
    evidence = exception_diagnostic(caught.value)
    assert evidence["reason_code"] == "yaml_parse_error"
    assert evidence["source_kind"] == source_kind
    assert evidence["source_line"] == 2 and evidence["source_column"] == 1
    assert "sensitive" not in str(caught.value) + json.dumps(evidence)
    assert "private-source" not in str(caught.value) + json.dumps(evidence)


def test_metadata_json_parser_location_without_rejected_text(tmp_path):
    path = tmp_path / "private-metadata.json"
    path.write_text('{"version": 1,\n"models": [sensitive-value]}')
    with pytest.raises(ConfigError) as caught:
        load_flow_metadata(path)
    assert exception_diagnostic(caught.value) == {
        "reason_code": "model_metadata_parse_error", "source_kind": "model_metadata",
        "source_line": 2, "source_column": 12,
    }
    assert "sensitive" not in str(caught.value)


def valid_model():
    return {"base_url": "https://omni.example", "model_id": "model-1", "model_path": "omni/model-1"}


@pytest.mark.parametrize("second,key,expected,reason", [
    ({"base_url": "https://omni.example", "model_path": "omni/second"},
     "models.model_id", "non_empty_string", "model_metadata_invalid_type"),
    ({**valid_model(), "sensitive-field": "sensitive-value"},
     "models", "mapping", "model_metadata_unknown_key"),
    ("sensitive-value", "models", "mapping", "model_metadata_invalid_type"),
    (valid_model(), "models.model_id", "string", "model_metadata_invalid_value"),
])
def test_metadata_entry_index_and_builtin_key(tmp_path, second, key, expected, reason):
    path = tmp_path / "flow.json"
    path.write_text(json.dumps({"version": 1, "models": [valid_model(), second]}))
    with pytest.raises(ConfigError) as caught:
        load_flow_metadata(path)
    evidence = exception_diagnostic(caught.value)
    assert evidence == {
        "reason_code": reason, "source_kind": "model_metadata", "entry_index": 2,
        "config_key": key, "expected_type": expected,
    }
    assert key in CONFIG_DIAGNOSTIC_KEYS
    assert "sensitive" not in str(caught.value) + json.dumps(evidence)


def test_metadata_unsafe_origin_retains_security_class_and_safe_context(tmp_path):
    path = tmp_path / "flow.json"
    path.write_text(json.dumps({"version": 1, "models": [{**valid_model(), "base_url": "http://localhost/private"}]}))
    with pytest.raises(SecurityPolicyError) as caught:
        load_flow_metadata(path)
    assert exception_diagnostic(caught.value) == {
        "reason_code": "model_metadata_invalid_value", "source_kind": "model_metadata", "entry_index": 1,
        "config_key": "models.base_url", "expected_type": "https_url",
    }


@pytest.mark.parametrize("reader,reason", [(None, "model_metadata_missing"),
                                           (ConfigError("sensitive-details"), "model_metadata_inaccessible")])
def test_metadata_missing_is_distinct_from_unreadable(reader, reason):
    with patch("omniflow.discovery.read_trusted_repo_text") as read:
        if isinstance(reader, Exception):
            read.side_effect = reader
        else:
            read.return_value = reader
        with pytest.raises(ConfigError) as caught:
            load_flow_metadata()
    assert exception_diagnostic(caught.value) == {"reason_code": reason, "source_kind": "model_metadata"}


def test_inaccessible_policy_is_not_reported_missing():
    with patch("omniflow.config.read_trusted_repo_text", side_effect=ConfigError("private")):
        with pytest.raises(ConfigError) as caught:
            load_config()
    assert exception_diagnostic(caught.value) == {"reason_code": "policy_inaccessible", "source_kind": "policy"}


@pytest.mark.parametrize("require,reason", [
    (require_api_key, "missing_api_key"),
    (require_repair_api_key, "missing_repair_api_key"),
    (require_sync_api_key, "missing_sync_api_key"),
])
def test_missing_credential_is_a_typed_failure_without_credential_values(require, reason):
    with pytest.raises(ConfigError) as caught:
        require()
    assert exception_diagnostic(caught.value) == {"reason_code": reason}


def test_unavailable_trusted_base_is_not_absent_file(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    with patch("omniflow.trust.github_event_payload", return_value={}), \
            patch("omniflow.revision_data.read_git_text", side_effect=ConfigError("missing revision")):
        with pytest.raises(ConfigError) as caught:
            read_trusted_repo_text(".omni/flow.json")
    assert caught.value.reason_code == "trusted_file_unavailable"
    with patch("omniflow.trust.github_event_payload", return_value={}), \
            patch("omniflow.revision_data.read_git_text", return_value=None):
        assert read_trusted_repo_text(".omni/flow.json") is None


def test_unavailable_git_comparisons_fail_but_genuine_empty_inventory_skips():
    failure = subprocess.CalledProcessError(128, ["git", "diff"])
    with patch("omniflow.discovery.subprocess.run", side_effect=failure):
        with pytest.raises(ConfigError) as caught:
            get_changed_files()
    assert caught.value.reason_code == "changed_files_unavailable"
    reset_changed_files_cache()
    with patch("omniflow.discovery.subprocess.run", side_effect=[failure, SimpleNamespace(stdout="")]):
        assert get_changed_files() == []
