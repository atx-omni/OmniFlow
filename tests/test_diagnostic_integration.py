import json

import pytest

from omniflow import cli
from omniflow.config import _to_config
from omniflow.diagnostics import public_diagnostic
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAuthError
from omniflow.execution import ValidationProgress
from omniflow.github.annotations import public_annotation_lines
from omniflow.omni_client import OmniClient
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif


def test_candidate_transport_cause_reaches_every_public_format_without_raw_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _to_config({}, source="test")
    context = ModelContext(model_id="private-model", base_url="https://synthetic.omniapp.co", model_path="omni",
                           branch_id="private-branch")
    progress = ValidationProgress(config, branch_id=context.branch_id, enforce_breaking_hold=False)
    progress.begin("candidate")
    error = OmniAuthError("RAW secret diagnosis SQL token", reason_code="omni_forbidden", diagnostic_context={
        "service": "omni", "api_operation": "model_yaml", "http_status": 403, "attempt_count": 1,
        "validation_scope": "base", "unexpected": "RAW secret diagnosis SQL token",
    })
    error.candidate_reason = "api_snapshot_unavailable"
    progress.fail(error)
    error.omniflow_progress = progress
    report, code = cli._write_context_failure_artifacts(config=config, context=context, output_dir=tmp_path, exc=error)
    safe = public_diagnostic(report)
    issue = safe["issues"][0]
    assert code == 3 and issue["http_status"] == 403
    assert issue["candidate_reason"] == "api_snapshot_unavailable"
    assert issue["reason_code"] == "omni_forbidden"
    assert issue["diagnostic_code"] == "OF-OMNI-FORBIDDEN"
    assert any(state.get("blocked_by") == "candidate" for state in safe["check_states"])
    outputs = [json.dumps(safe), render_markdown_report(safe), json.dumps(to_sarif(safe)), to_junit(safe),
               "\n".join(public_annotation_lines(safe["issues"], limit=5))]
    for output in outputs:
        assert "OF-OMNI-FORBIDDEN" in output
        assert "RAW secret" not in output and "private-model" not in output
    assert "locations" not in to_sarif(safe)["runs"][0]["results"][0]
    assert "file=" not in outputs[-1] and "line=1" not in outputs[-1]


@pytest.mark.parametrize("state", ["partial", "unavailable", "skipped"])
def test_incomplete_optional_check_is_not_reported_as_complete_even_with_zero_exit(state, capsys):
    progress = ValidationProgress(_to_config({}, source="test"), branch_id=None, enforce_breaking_hold=False)
    for name, entry in progress.states.items():
        if entry["status"] != "disabled":
            progress.complete(name)
    progress.complete("model", execution_status=state, skipped_reason="no_branch" if state == "skipped" else None)
    assert progress.evidence()["validation_complete"] is False
    safe = public_diagnostic({"validation_complete": False, "issues": [], "check_states": progress.evidence()["check_states"]})
    assert '<skipped message="Incomplete:' in to_junit(safe)
    assert capsys.readouterr().out == ""  # Progress cannot corrupt route's stdout protocol.


def test_unknown_refresh_outcome_keeps_reconciliation_action_and_underlying_http_reason():
    safe = public_diagnostic({"issues": [{"validator": "dbt_sync", "severity": "error",
        "reason_code": "dbt_refresh_status_unknown", "cause_code": "omni_service_unavailable",
        "http_status": 503, "operation_phase": "poll", "last_observed_status": "RUNNING"}]})
    issue = safe["issues"][0]
    assert issue["diagnostic_code"] == "OF-DBT-REFRESH-STATUS-UNKNOWN"
    assert "Do not start another refresh" in issue["next_action"]
    assert issue["cause_code"] == "omni_service_unavailable"


def test_doctor_path_and_branch_identity_are_case_sensitive():
    context = ModelContext(model_id="synthetic", base_url="https://synthetic.omniapp.co",
                           model_path="omni/Analytics Demo", base_branch="Main")
    assert cli._git_configuration_issues(context, {"modelPath": "omni/analytics demo", "baseBranch": "main"})
    assert cli._git_configuration_issues(context, {"modelPath": "omni/Analytics Demo", "baseBranch": "Main"}) == []
    assert cli._git_configuration_issues(context, {})  # Missing evidence is not verified alignment.


def test_public_projection_rejects_dynamic_keys_versions_urls_and_prose():
    safe = public_diagnostic({"tool_version": "1patient_secret", "issues": [{
        "validator": "setup", "severity": "error", "reason_code": "config_invalid_type",
        "config_key": "patient name", "expected_type": "boolean", "source_line": 4,
        "http_status": 123456789, "api_operation": "https://private.invalid", "message": "secret prose",
    }]})
    text = json.dumps(safe)
    for excluded in ("1patient_secret", "patient name", "123456789", "private.invalid", "secret prose"):
        assert excluded not in text
    assert safe["issues"][0]["source_line"] == 4


@pytest.mark.parametrize("ids,reason", [(["a", "b"], "branch_ambiguous"), ([None], "branch_identity_invalid")])
def test_branch_resolution_never_picks_first_ambiguous_or_malformed_match(monkeypatch, ids, reason):
    client = OmniClient(base_url="https://synthetic.omniapp.co", api_key="synthetic")
    monkeypatch.setattr(client, "list_models", lambda **kwargs: [
        {"modelKind": "BRANCH", "baseModelId": "base", "name": "candidate", "id": item} for item in ids
    ])
    with pytest.raises(ConfigError) as failure:
        client.resolve_branch_id("base", "candidate")
    assert failure.value.reason_code == reason
