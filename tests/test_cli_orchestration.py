import unittest
from unittest import mock

from omniflow.cli import _client_and_branch_for_context, _git_configuration_issues
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError


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


if __name__ == "__main__":
    unittest.main()
