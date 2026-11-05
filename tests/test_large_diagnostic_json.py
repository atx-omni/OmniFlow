import json

from omniflow.diagnostics import public_diagnostic
from omniflow.github.report_publisher import _read_json
from omniflow.reporting import json_report
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.notification import render_pr_comment


def _legacy(issue):
    return {key: value for key, value in issue.items() if key not in json_report.RECOVERY_FIELDS}


def test_customer_scale_nested_report_still_reaches_pr_comment(tmp_path):
    source = public_diagnostic({"policy_decision": "fail", "exit_code": 2, "validation_complete": False, "issues": [
        {"validator": "candidate", "type": "candidate_verification_failed", "severity": "error", "blocking": True,
         "validation_scope": "branch", "candidate_reason": "content_mismatch"},
        {"validator": "model", "type": "model_validation_issue", "severity": "error", "blocking": False,
         "state": "existing"},
    ]})
    blocker, advisory = source["issues"]
    issues = [blocker, *[advisory] * 6500]
    source.update(issues=issues, model_reports=[{"issues": issues, "check_reports": [{"issues": issues}]}])
    # This fixture crosses the reader's existing budget only with inline recovery fields.
    legacy_issues = [_legacy(issue) for issue in issues]
    legacy = dict(source, issues=legacy_issues,
                  model_reports=[{"issues": legacy_issues, "check_reports": [{"issues": legacy_issues}]}])
    assert len(json.dumps(legacy, indent=2)) < json_report.MAX_PUBLIC_REPORT_BYTES < len(json.dumps(source, indent=2))

    target = tmp_path / "report.json"
    json_report.write_json_report(target, source)
    loaded = _read_json(target)
    assert len(loaded["recovery_catalog"]) == 2
    for occurrences in (loaded["issues"], loaded["model_reports"][0]["issues"],
                        loaded["model_reports"][0]["check_reports"][0]["issues"]):
        assert len(occurrences) == 6501
        assert occurrences[0]["blocking"] and not occurrences[-1]["blocking"]
        assert loaded["recovery_catalog"][occurrences[0]["recovery_ref"]]["recovery_steps"] == blocker["recovery_steps"]
        assert {k: v for k, v in occurrences[-1].items() if k != "recovery_ref"} == _legacy(advisory)
    # Renderers derive instructions from recorded facts, never from an edited catalog.
    loaded["recovery_catalog"][loaded["issues"][0]["recovery_ref"]]["recovery_steps"] = ["INJECTED_ACTION"]
    pr = render_pr_comment(loaded, head_sha="a" * 40, run_url="https://github.com/test/demo/actions/runs/12")
    assert "Blocking findings: 1" in pr and "Nonblocking findings: 6,500" in pr
    assert blocker["recovery_steps"][0] in pr and "INJECTED_ACTION" not in pr
    markdown = render_markdown_report(loaded)
    assert "How to resolve it" in markdown and loaded["run_reference"] in markdown
    assert "INJECTED_ACTION" not in markdown


def test_near_limit_keeps_legacy_evidence_when_even_a_catalog_wont_fit(monkeypatch):
    source = public_diagnostic({"issues": [{"validator": "candidate", "type": "candidate_verification_failed",
                                          "severity": "error", "candidate_reason": "content_mismatch"}]})
    legacy = dict(source, issues=[_legacy(source["issues"][0])])
    monkeypatch.setattr(json_report, "MAX_PUBLIC_REPORT_BYTES", len(json.dumps(legacy, indent=2)) + 1)
    serialized = json_report.diagnostic_json(source)
    assert len(serialized) <= json_report.MAX_PUBLIC_REPORT_BYTES
    assert json.loads(serialized) == legacy
    assert "How to resolve it" in render_markdown_report(json.loads(serialized))
