"""Only synthetic payloads: malformed evidence must not become a clean check."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree

import pytest

from omniflow.cli import build_parser, cmd_run
from omniflow.config import _to_config
from omniflow.discovery import ModelContext
from omniflow.validators.content import ContentEvidenceError, extract_issues, run_content_validation

PRIVATE = "synthetic-private-nested-evidence"


def document(messages=(), **extra):
    return {"content": [{
        "document_id": "synthetic-physical", "identifier": "synthetic-doc",
        "name": "Synthetic private title", "dashboard_filter_issues": [],
        "queries_and_issues": [{"query_presentation_id": "synthetic-query", "issues": list(messages), **extra}],
    }]}


@pytest.mark.parametrize("key", ["issues", "validation_issues", "errors", "documents", "items", "results"])
@pytest.mark.parametrize("messages", [[], ["Existing issue"]])
def test_single_legacy_envelope_and_metadata_remain_supported(key, messages):
    assert extract_issues({key: messages, "request_id": "synthetic", "metadata": {"errors": 3}}) == messages


def test_message_metadata_empty_results_and_optional_omissions_remain_supported():
    assert extract_issues({"content": [], "branch": None, "model_id": "synthetic"}) == []
    assert extract_issues(document()) == []
    leaf = {"message": "Existing issue", "code": "test", "metadata": {"errors": ["not evidence"]}}
    assert extract_issues(document([leaf]))[0]["message"] == "Existing issue"
    optional = document()
    optional["content"][0]["queries_and_issues"][0].pop("issues")
    assert extract_issues(optional) == []


@pytest.mark.parametrize("malformed", [
    {"content": [], "errors": [PRIVATE]}, {"issues": [], "errors": [PRIVATE]},
    {"content": [], "issues": []}, {"content": [], "issues": None},
    document([{"message": "Existing issue", "issues": [PRIVATE]}]),
    document([{"message": "Existing issue", "errors": [PRIVATE]}]),
    document([{"message": "Existing issue", "validation_issues": None}]),
    document([], errors=[PRIVATE]), document([], validation_issues=[PRIVATE]),
])
@pytest.mark.parametrize("scope", ["branch", "base"])
def test_ambiguous_evidence_blocks_before_history_or_label_filtering(tmp_path, malformed, scope):
    client = SimpleNamespace(
        validate_content=lambda *a, branch_id=None, **kw: copy.deepcopy(
            malformed if (bool(branch_id) == (scope == "branch")) else {"content": []}
        ),
        list_content=lambda **kw: [],
    )
    with pytest.raises(ContentEvidenceError) as caught:
        run_content_validation(
            client=client, model_id="model", branch_id="branch", user_id=None,
            include_personal_folders=False, labels=["Unselected"], fail_on_new_only=True,
            history_in=tmp_path / "missing.json", history_out=tmp_path / "history.json",
            report_out=tmp_path / "report.json",
        )
    finding = caught.value.report_issue()
    assert caught.value.exit_code == 4
    assert finding["reason_code"] == "unsupported_issue_shape"
    assert finding["comparison_status"] == "unavailable" and finding["validation_scope"] == scope
    assert finding["regression_status"] == finding["content_health"] == "unknown"
    assert finding["coverage_complete"] is False and finding["blocking"] is True
    assert PRIVATE not in json.dumps(finding)
    assert not (tmp_path / "history.json").exists() and not (tmp_path / "report.json").exists()


@pytest.mark.parametrize("scope", ["branch", "base"])
def test_cli_ambiguous_envelope_reports_safe_incomplete_evidence(tmp_path, monkeypatch, capsys, scope):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    config = _to_config({}, None)
    config.content_validation.fail_on_new_only = True
    context = ModelContext(base_url="https://synthetic.example", model_id="model", model_path="model", branch_id="branch")
    client = SimpleNamespace(
        validate_content=lambda *a, branch_id=None, **kw: (
            {"content": [], "errors": [PRIVATE]} if (bool(branch_id) == (scope == "branch")) else {"content": []}
        ), list_content=lambda **kw: [],
    )
    args = build_parser().parse_args(["run", "--auto"])
    with patch("omniflow.cli.load_config", return_value=config), \
            patch("omniflow.cli.discover_contexts", return_value=[context]), \
            patch("omniflow.cli._client_and_branch_for_context", return_value=(client, "branch")):
        assert cmd_run(args, changed_files=[]) == 4
    public = tmp_path / ".omniflow/public"
    report = json.loads((public / "report.json").read_text())
    assert report["exit_code"] == 4 and report["validation_complete"] is False
    assert report["issues"][0]["type"] == "content_evidence_unavailable"
    assert report["issues"][0]["validation_scope"] == scope
    assert report["model_reports"][0]["check_reports"][0]["coverage_complete"] is False
    assert json.loads((public / "report.sarif").read_text())["runs"][0]["results"][0]["level"] == "error"
    assert ElementTree.fromstring((public / "junit.xml").read_text()).get("failures") == "1"
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err + "".join(p.read_text() for p in public.iterdir())
