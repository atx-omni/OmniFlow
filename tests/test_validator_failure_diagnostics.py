"""Bounded diagnostic contracts: known cause, honest coverage, safe recovery."""
from unittest.mock import Mock

import pytest

from omniflow.config import ContractSettings, DbtExposureSettings
from omniflow.contracts import evaluate_contracts
from omniflow.dbt_sync import run_dbt_sync
from omniflow.downstream import generate_downstream_dependencies
from omniflow.exceptions import OmniAPIError
from omniflow.exposures import run_dbt_exposure_enrichment
from omniflow.repair.orchestrator import _validation_summary
from omniflow.validators.ai_eval import run_ai_eval_validation
from omniflow.validators.content import _report_issue
from omniflow.validators.model import run_model_validation
from omniflow.validators.yaml_lint import DEFAULT_RULES, _emit_diff_issue


def transport_failure():
    return OmniAPIError(
        "untrusted remote text must not become a public diagnostic",
        reason_code="omni_service_unavailable",
        diagnostic_context={"service": "omni", "http_status": 503, "retryable": True},
    )


@pytest.mark.parametrize("records", [
    [{"dashboard_identifier": "d", "exposure": "invalid"}],
    [{"dashboard_identifier": "d", "exposure": []}],
    [{"dashboard_identifier": "d", "exposure": {}}],
    [{"name": "d", "depends_on": "model.orders"}],
    [None],
])
def test_malformed_exposure_never_reports_available(records):
    client = Mock()
    client.get_dbt_exposures.return_value = {"records": records}
    report, code = run_dbt_exposure_enrichment(
        client=client, model_id="m", branch_id="b",
        settings=DbtExposureSettings(enabled=True, fail_on_unavailable=True),
    )
    assert code == 1
    assert report["execution_status"] == "unavailable"
    assert report["summary"]["coverage_status"] == "unavailable"
    assert report["issues"][0]["cause_code"] == "invalid_dbt_exposure_response"


@pytest.mark.parametrize("records,status", [
    ([], "completed"),
    ([{"dashboard_identifier": "d", "exposure": None}], "partial"),
    ([{"dashboard_identifier": "d", "exposure": {"name": "d", "depends_on": []}}], "completed"),
])
def test_valid_empty_unmapped_and_mapped_exposures_remain_distinct(records, status):
    client = Mock()
    client.get_dbt_exposures.return_value = {"records": records}
    report, code = run_dbt_exposure_enrichment(
        client=client, model_id="m", branch_id="b", settings=DbtExposureSettings(enabled=True),
    )
    assert code == 0
    assert report["execution_status"] == status
    assert report["coverage_complete"] is (status == "completed")


def eval_run(client, branch_id="b", prompt_sets=None):
    return run_ai_eval_validation(
        client=client, model_id="m", branch_id=branch_id,
        prompt_sets=[{"id": "set"}] if prompt_sets is None else prompt_sets,
        fail_on_regression=True, poll_interval_seconds=1, timeout_seconds=10, scoring_grace_seconds=1,
    )


def test_ai_failure_keeps_typed_cause_in_public_candidate_and_restricted_evidence():
    client = Mock()
    client.get_ai_eval_prompt_set.side_effect = transport_failure()
    report, detail, code = eval_run(client)
    assert code == 4
    assert report["execution_status"] == "unavailable"
    issue = report["issues"][0]
    assert issue["reason_code"] == "ai_eval_request_failed"
    assert issue["cause_code"] == "omni_service_unavailable"
    assert issue["http_status"] == 503
    assert issue["operation_phase"] == "prompt_set_preflight"
    assert issue["coverage_complete"] is False
    assert detail["failure"] == issue
    assert "untrusted remote text" not in str(report) + str(detail)


@pytest.mark.parametrize("branch_id,sets,reason", [
    (None, [{"id": "set"}], "no_branch"),
    ("b", [{"id": "set", "model_id": "other"}], "no_matching_prompt_set"),
])
def test_ai_skip_is_explicit_without_claiming_comparison(branch_id, sets, reason):
    client = Mock()
    report, _, code = eval_run(client, branch_id=branch_id, prompt_sets=sets)
    assert code == 0
    assert report["execution_status"] == "skipped"
    assert report["skipped_reason"] == reason
    assert report["validation_complete"] is False
    client.get_ai_eval_prompt_set.assert_not_called()


def test_model_unavailable_baseline_retains_cause_and_does_not_pass():
    client = Mock()
    client.validate_model.side_effect = [transport_failure(), []]
    report, code = run_model_validation(client=client, model_id="m", branch_id="b", fail_on_new_only=True)
    assert code == 4
    assert report["execution_status"] == "unavailable"
    assert report["issues"][0]["cause_code"] == "omni_service_unavailable"
    assert report["issues"][0]["validation_scope"] == "base"


def test_started_refresh_poll_failure_preserves_job_without_restarting():
    client = Mock()
    client.start_schema_refresh.return_value = {"job_id": "started-job", "status": "RUNNING"}
    client.get_schema_refresh_job_status.side_effect = transport_failure()
    report, code = run_dbt_sync(
        client=client, model_id="m", branch_id=None, refresh_mode="hard",
        poll_interval_seconds=2, timeout_seconds=30, sleep=lambda _: None, monotonic=lambda: 0,
    )
    assert code == 4
    assert report["status"] == "status_unknown"
    assert report["execution_status"] == "unavailable"
    assert report["job_id"] == "started-job"
    assert report["last_observed_status"] == "RUNNING"
    assert report["issues"][0]["cause_code"] == "omni_service_unavailable"
    assert report["operation_phase"] == "poll"
    assert report["polls"] == 1
    client.start_schema_refresh.assert_called_once()


def test_downstream_and_contracts_preserve_incomplete_coverage_and_cause():
    client = Mock()
    client.search_content_references.side_effect = transport_failure()
    diff = {"changes": [{"type": "field_deleted", "field": "orders.id", "risk": "breaking"}]}
    dependencies = generate_downstream_dependencies(client=client, model_id="m", branch_id="b", diff_result=diff)
    assert dependencies["execution_status"] == "unavailable"
    assert dependencies["coverage_complete"] is False
    report, code = evaluate_contracts(
        diff_result=diff, dependencies=dependencies, settings=ContractSettings(enabled=True), model_id="m",
    )
    assert code == 1
    assert report["execution_status"] == "partial"
    assert report["coverage_complete"] is False
    assert report["issues"][-1]["cause_code"] == "omni_service_unavailable"


def test_lint_diff_keeps_field_and_previous_identity_separate_from_message():
    issues = []
    _emit_diff_issue(issues, DEFAULT_RULES, {
        "type": "field_deleted", "file": "orders.view", "field": "orders.id", "view": "orders",
        "previous_field": "orders.old_id", "message": "private issue detail",
    })
    assert issues[0]["field"] == "orders.id"
    assert issues[0]["previous_field"] == "orders.old_id"
    assert issues[0]["view"] == "orders"


@pytest.mark.parametrize("kind,reason", [
    ("query", "content_query_validation_issue"),
    ("dashboard_filter", "content_dashboard_filter_validation_issue"),
    ("invented remote category", "content_validation_issue"),
])
def test_content_categories_use_structure_not_message_guesses(kind, reason):
    issue = _report_issue({"raw": {"issue_type": kind}, "summary": "Unknown field or permission denied?"},
                          state="new", severity="error")
    assert issue["reason_code"] == reason


def test_repair_failure_summary_retains_bounded_typed_findings_without_remote_messages():
    issue = {"validator": "content", "severity": "error", "reason_code": "content_query_validation_issue",
             "message": "private issue detail", "raw": {"secret": "value"}}
    summary = _validation_summary({"issues": [issue] * 25, "policy_decision": "fail"}, 1)
    assert summary["failed_finding_count"] == 25
    assert summary["failed_check_count"] == 1
    assert len(summary["issues"]) == 20
    assert summary["issues"][0]["reason_code"] == "content_query_validation_issue"
    assert "private issue detail" not in str(summary)
    assert "secret" not in str(summary)
