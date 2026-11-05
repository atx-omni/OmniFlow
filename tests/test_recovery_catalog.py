from pathlib import Path

import pytest

from omniflow.diagnostic_contract import REASON_DETAILS
from omniflow.diagnostics import VALIDATORS, public_diagnostic
from omniflow.troubleshooting import GUIDE_ANCHORS, guide_url


def test_every_reported_family_and_reason_has_a_real_recovery_destination():
    guide = (Path(__file__).resolve().parents[1] / "docs" / "ERROR_CODES.md").read_text()
    for anchor in GUIDE_ANCHORS:
        assert f'id="guide-{anchor}"' in guide
    findings = [{"validator": name, "type": "validation_execution_failed", "severity": "error"} for name in VALIDATORS]
    findings += [{"validator": "execution", "reason_code": reason, "severity": "error"} for reason in REASON_DETAILS]
    for issue in public_diagnostic({"issues": findings})["issues"]:
        assert 1 <= len(issue["recovery_steps"]) <= 3
        assert issue["guide_anchor"] in GUIDE_ANCHORS
        assert issue["verify_recovery"] and issue["support_action"] and issue["outcome_label"]


@pytest.mark.parametrize("evidence,expected", [
    ({"blocking": True, "state": "new"}, "before editing"),
    ({"blocking": True, "state": "existing"}, "blocks under that policy"),
    ({"blocking": False, "state": "existing"}, "separately"),
    ({"blocking": False, "health_only": True}, "primary blocker"),
])
def test_the_same_model_error_can_require_different_actions(evidence, expected):
    issue = {"validator": "model", "type": "model_validation_issue", "severity": "error",
             "reason_code": "model_validation_issue", **evidence}
    projected = public_diagnostic({"issues": [issue]})["issues"][0]
    assert expected in " ".join(projected["recovery_steps"])
    assert (projected["outcome_label"] == "Informational") is (not evidence["blocking"])


def test_guides_only_link_to_known_code_repositories_at_valid_tool_revisions():
    pin = "abcde" * 8
    assert f"/atx-omni/OmniFlow/blob/{pin}/" in guide_url("setup", pin, "atx-omni/OmniFlow")
    assert guide_url("../bad", pin) == ""
    url = guide_url("setup", "../fake", "example.com/other")
    assert url == "https://github.com/exploreomni/OmniFlow/blob/main/docs/ERROR_CODES.md#guide-setup"


@pytest.mark.parametrize("total,compared,phrase", [
    (1, 1, "The only file matched by name"),
    (2, 2, "Only the last file differs"),
    (2, 1, "1 file unchecked"),
])
def test_first_mismatch_uses_the_observed_scan_boundary(total, compared, phrase):
    issue = {"type": "candidate_verification_failed", "validator": "candidate", "severity": "error",
             "candidate_reason": "content_mismatch", "git_file_count": total, "omni_file_count": total,
             "git_only_count": 0, "omni_only_count": 0, "compared_file_count": compared}
    assert phrase in public_diagnostic({"issues": [issue]})["issues"][0]["coverage_description"]
