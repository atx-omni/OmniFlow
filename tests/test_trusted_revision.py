from unittest.mock import patch

import pytest

from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.trust import read_trusted_repo_text


@pytest.mark.parametrize("local_result,remote_text", [
    (ConfigError("Missing shallow object"), "checks: {}\n"),
    (ConfigError("Missing shallow object"), None),
])
def test_shallow_checkout_reads_only_the_immutable_trusted_base(monkeypatch, local_result, remote_text):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    base = "a" * 40
    event = {"pull_request": {"base": {"sha": base}}}
    with patch("omniflow.trust.github_event_payload", return_value=event), \
            patch("omniflow.revision_data.pull_request_revision", return_value=(base, "test/models")), \
            patch("omniflow.revision_data.read_git_text", side_effect=local_result), \
            patch("omniflow.revision_data.read_github_text", return_value=remote_text) as remote:
        assert read_trusted_repo_text(".omniflow.yml", max_bytes=1024) == remote_text
    remote.assert_called_once_with("test/models", base, ".omniflow.yml", max_bytes=1024)


def test_local_security_failure_does_not_fall_back_to_another_reader(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    base = "a" * 40
    with patch("omniflow.trust.github_event_payload", return_value={"pull_request": {"base": {"sha": base}}}), \
            patch("omniflow.revision_data.pull_request_revision", return_value=(base, "test/models")), \
            patch("omniflow.revision_data.read_git_text", side_effect=SecurityPolicyError("Unsafe entry")), \
            patch("omniflow.revision_data.read_github_text") as remote:
        with pytest.raises(SecurityPolicyError):
            read_trusted_repo_text(".omniflow.yml")
    remote.assert_not_called()
