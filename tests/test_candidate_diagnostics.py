import json

import pytest

from omniflow.artifacts import write_public_reports
from omniflow.diagnostics import public_diagnostic
from omniflow.github.annotations import annotation_lines


@pytest.mark.parametrize("reason,phrase", [
    ("file_inventory_mismatch", "inventories differ"),
    ("ambiguous_file_identity", "Multiple files map"),
    ("content_mismatch", "contents differ"),
    ("changed_during_validation", "changed during validation"),
    ("api_snapshot_unavailable", "Omni authored YAML could not be retrieved"),
    ("git_snapshot_unavailable", "Git snapshot could not be retrieved"),
])
def test_reason_specific_guidance_and_counts_are_private_safe_in_every_format(tmp_path, reason, phrase):
    private = "PRIVATE-CUSTOMER-PATH-AND-CONTENT"
    report = {"exit_code": 2, "validation_complete": False, "issues": [{
        "validator": "candidate", "type": "candidate_verification_failed", "severity": "error",
        "candidate_reason": reason, "validation_scope": "branch", "blocking": True,
        "git_file_count": 37, "omni_file_count": 38, "git_only_count": 1, "omni_only_count": 2,
        "message": private, "git_only_paths": [private], "omni_only_paths": [private],
        "file": private, "model_id": private, "raw": private,
    }]}
    safe = write_public_reports(report, output_dir=tmp_path, formats=["json", "markdown", "sarif", "junit"],
                                redaction_level="standard")
    issue = safe["issues"][0]
    assert phrase in issue["message"]
    assert "Git files: 37, Omni files: 38, Git-only: 1, Omni-only: 2" in issue["message"]
    assert issue["git_file_count"] == 37
    for path in (tmp_path / "public").iterdir():
        assert private not in path.read_text()
    output = "\n".join(annotation_lines(safe["issues"]))
    assert "Git files: 37" in output and private not in output


@pytest.mark.parametrize("value", [-1, 2**31, True, "PRIVATE", {"path": "PRIVATE"}])
def test_invalid_counts_cannot_enter_shareable_diagnostics(value):
    safe = public_diagnostic({"issues": [{
        "validator": "candidate", "type": "candidate_verification_failed", "candidate_reason": "content_mismatch",
        "git_file_count": value, "omni_file_count": value, "git_only_count": value, "omni_only_count": value,
    }]})
    assert "git_file_count" not in safe["issues"][0]
    assert "Counts:" not in safe["issues"][0]["message"]
    assert "PRIVATE" not in json.dumps(safe)
