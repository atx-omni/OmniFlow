import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.cli import _client_and_branch_for_context, _git_configuration_issues, _write_context_failure_artifacts
from omniflow.config import load_config
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAPIError


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def resolve_branch_id(self, model_id, branch_name):
        return None


class CliOrchestrationTests(unittest.TestCase):
    def test_branch_name_without_resolved_branch_id_fails_closed(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model",
            branch_name="feature/a",
        )
        with mock.patch("omniflow.cli.require_api_key", return_value="secret"):
            with mock.patch("omniflow.cli.OmniClient", FakeClient):
                with self.assertRaises(ConfigError):
                    _client_and_branch_for_context(context, timeout=60)

    def test_git_configuration_issues_detect_metadata_drift(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model",
            base_branch="main",
            git_provider="github",
            web_url="https://github.com/acme/repo",
        )
        issues = _git_configuration_issues(
            context,
            {
                "modelPath": "omni/other",
                "baseBranch": "main",
                "gitServiceProvider": "github",
                "webUrl": "https://github.com/acme/repo",
            },
        )
        self.assertEqual(issues, ["model_path expected 'omni/model' but Omni reports 'omni/other'"])

    def test_git_configuration_issues_pass_when_metadata_matches(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model/",
            base_branch="main",
            git_provider="github",
            web_url="https://github.com/acme/repo/",
        )
        issues = _git_configuration_issues(
            context,
            {
                "modelPath": "omni/model",
                "baseBranch": "main",
                "gitServiceProvider": "github",
                "webUrl": "https://github.com/acme/repo",
            },
        )
        self.assertEqual(issues, [])

    def test_context_failure_writes_report_artifact(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model",
            branch_name="feature/a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            report, exit_code = _write_context_failure_artifacts(
                config=load_config(None),
                context=context,
                output_dir=Path(tmp),
                exc=OmniAPIError("Omni API request failed"),
            )
            self.assertTrue((Path(tmp) / "report.json").exists())
        self.assertEqual(exit_code, 4)
        self.assertEqual(report["exit_code_reason"], "Omni API error")
        self.assertEqual(report["issues"][0]["validator"], "context")


if __name__ == "__main__":
    unittest.main()
