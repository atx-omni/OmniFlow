from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omniflow.candidate import CandidateVerificationError
from omniflow.validation_run import mark_health_only, may_advise, verify_with_retry


def test_sync_retry_is_bounded_and_reuses_identical_scope():
    error = CandidateVerificationError("file_inventory_mismatch")
    with patch("omniflow.validation_run.verify_candidate", side_effect=[error, {"status": "verified"}]) as verify, \
            patch("omniflow.validation_run.time.sleep") as sleep:
        assert verify_with_retry("client", "context", revision_side="base") == {"status": "verified"}
    assert verify.call_count == 2
    assert verify.call_args_list[0] == verify.call_args_list[1]
    sleep.assert_called_once_with(2)


def test_unsafe_candidate_is_never_retried():
    with patch("omniflow.validation_run.verify_candidate",
               side_effect=CandidateVerificationError("unsafe_git_entry")) as verify, \
            patch("omniflow.validation_run.time.sleep") as sleep:
        with pytest.raises(CandidateVerificationError):
            verify_with_retry("client", "context")
    assert verify.call_count == 1
    sleep.assert_not_called()


def test_final_drift_is_not_retried_away():
    with patch("omniflow.validation_run.verify_candidate",
               side_effect=[CandidateVerificationError("content_mismatch"), {"status": "verified"}]) as verify, \
            patch("omniflow.validation_run.time.sleep") as sleep:
        with pytest.raises(CandidateVerificationError):
            verify_with_retry("client", "context", retry=False)
    assert verify.call_count == 1
    sleep.assert_not_called()


def test_health_is_not_misrepresented_as_a_regression():
    issue = {"severity": "error", "blocking": True, "state": "new"}
    report = {"issues": [issue], "new_issues": 1, "validation_complete": True}
    mark_health_only(report)
    assert report["regression_status"] == "unknown"
    assert report["validation_complete"] is False
    assert "new_issues" not in report
    assert "state" not in issue
    assert issue["blocking"] is False


@pytest.mark.parametrize("mode,reason,allowed", [
    ("strict", "file_inventory_mismatch", False),
    ("advisory", "file_inventory_mismatch", True),
    ("advisory", "unsafe_git_entry", False),
    ("advisory", "non_omni_source_conflict", False),
    ("advisory", "ambiguous_file_identity", False),
])
def test_advisory_cannot_waive_source_or_exclusion_safety(mode, reason, allowed):
    config = SimpleNamespace(validation=SimpleNamespace(mode=mode))
    assert may_advise(config, CandidateVerificationError(reason), []) is allowed
    assert not may_advise(config, CandidateVerificationError(reason), [{"operational_failure": True}])


def test_advisory_never_waives_independent_security_hold_or_evidence_blocker():
    config = SimpleNamespace(validation=SimpleNamespace(mode="advisory"))
    for issue in (
        {"validator": "semantic_lint", "rule_id": "forbid_personal_folder_validation_scope", "blocking": True},
        {"validator": "breaking_change_hold", "blocking": True},
        {"validator": "execution", "coverage_complete": False},
    ):
        assert not may_advise(config, CandidateVerificationError("content_mismatch"), [issue])


def test_health_conversion_preserves_operational_failures():
    report = {"issues": [{"severity": "error", "operational_failure": True, "blocking": True}]}
    mark_health_only(report)
    assert report["issues"][0]["blocking"]
    assert report["blocking_issues"] == 1


def test_advisory_cannot_skip_configured_deployment_hold():
    config = SimpleNamespace(validation=SimpleNamespace(mode="advisory"),
                             breaking_change_hold=SimpleNamespace(enabled=True))
    assert not may_advise(config, CandidateVerificationError("content_mismatch"), [])
