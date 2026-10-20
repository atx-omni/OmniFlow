import hashlib
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from omniflow import candidate, revision_data
from omniflow.candidate import CandidateVerificationError, verify_candidate
from omniflow.diagnostics import public_diagnostic
from omniflow.exceptions import (
    API_OPERATIONS,
    TRANSPORT_REASON_CODES,
    ConfigError,
    GitHubAPIError,
    GitHubAuthError,
    MissingPolicyError,
    OmniAPIError,
    OmniAuthError,
    OmniFlowError,
    OmniResponseError,
    SecurityPolicyError,
    exception_diagnostic,
)
from omniflow.github.revalidation import GitHubRepository
from omniflow.omni_client import OmniClient

PRIVATE = "synthetic-patient-name-and-private-object"


class Response:
    def __init__(self, status=200, body=b"[]", headers=None, stream_error=None):
        self.status_code = status
        self.ok = 200 <= status < 400
        self.headers = headers or {}
        self.body = body
        self.stream_error = stream_error
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        if self.stream_error:
            raise self.stream_error
        yield self.body

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def client_for(*, response=None, error=None):
    session = SimpleNamespace(headers={}, request=Mock(return_value=response, side_effect=error))
    client = OmniClient(base_url="https://omni.example", api_key="synthetic-key", session=session)
    return client, session


def assert_private_free(exc):
    detail = exception_diagnostic(exc)
    assert PRIVATE not in json.dumps(detail)
    assert "https://" not in json.dumps(detail)
    if "api_operation" in detail:
        assert detail["api_operation"] in API_OPERATIONS
    if "reason_code" in detail:
        assert detail["reason_code"] in TRANSPORT_REASON_CODES
    return detail


def test_exception_contract_preserves_legacy_reason_without_inventing_one():
    assert exception_diagnostic(MissingPolicyError())["reason_code"] == "missing_policy_config"
    assert exception_diagnostic(OmniResponseError("empty_api_response"))["reason_code"] == "empty_api_response"
    assert exception_diagnostic(ValueError(PRIVATE)) == {}
    context = {"service": "omni", "reason_code": "ignored"}
    error = OmniFlowError(PRIVATE, reason_code="omni_timeout", diagnostic_context=context, exit_code=4)
    context["service"] = "changed"
    assert exception_diagnostic(error) == {"service": "omni", "reason_code": "omni_timeout"}
    assert error.exit_code == 4


@pytest.mark.parametrize("status,reason,error_type,attempts", [
    (401, "omni_unauthorized", OmniAuthError, 1),
    (403, "omni_forbidden", OmniAuthError, 1),
    (404, "omni_http_error", OmniAPIError, 1),
    (429, "omni_rate_limited", OmniAPIError, 4),
    (503, "omni_service_unavailable", OmniAPIError, 4),
])
def test_omni_http_failures_keep_status_operation_and_retry_evidence(monkeypatch, status, reason, error_type, attempts):
    monkeypatch.setattr("omniflow.omni_client.time.sleep", lambda _: None)
    response = Response(status, PRIVATE.encode())
    client, session = client_for(response=response)
    with pytest.raises(error_type) as caught:
        client.validate_model(PRIVATE)
    detail = assert_private_free(caught.value)
    assert detail["reason_code"] == reason
    assert detail["api_operation"] == "model_validation"
    assert detail["http_status"] == status
    assert detail["attempt_count"] == attempts
    assert session.request.call_count == attempts
    assert response.closed


@pytest.mark.parametrize("error,reason", [
    (requests.Timeout(PRIVATE), "omni_timeout"),
    (requests.exceptions.SSLError(PRIVATE), "omni_tls_error"),
    (requests.ConnectionError(PRIVATE), "omni_connection_error"),
    (requests.RequestException(PRIVATE), "omni_request_failed"),
])
def test_omni_network_failure_categories_do_not_echo_exception(monkeypatch, error, reason):
    monkeypatch.setattr("omniflow.omni_client.time.sleep", lambda _: None)
    client, session = client_for(error=error)
    with pytest.raises(OmniAPIError) as caught:
        client.get_model_yaml(PRIVATE)
    assert assert_private_free(caught.value)["reason_code"] == reason
    assert PRIVATE not in str(caught.value)
    assert session.request.call_count == 4


@pytest.mark.parametrize("body,error,reason", [
    (b"", None, "empty_api_response"),
    (b"not json", None, "invalid_json_response"),
    (b'{"duplicate":1,"duplicate":2}', None, "invalid_json_response"),
    (b"", requests.Timeout(PRIVATE), "omni_response_interrupted"),
])
def test_body_failure_keeps_operation_without_adding_retries(body, error, reason):
    response = Response(body=body, stream_error=error)
    client, session = client_for(response=response)
    with pytest.raises(OmniAPIError) as caught:
        client.validate_content(PRIVATE)
    detail = assert_private_free(caught.value)
    assert (detail["reason_code"], detail["api_operation"], detail["attempt_count"]) == (reason, "content_validation", 1)
    assert response.closed and session.request.call_count == 1


def test_write_failure_is_not_retried_and_does_not_recommend_automatic_retry():
    client, session = client_for(error=requests.Timeout(PRIVATE))
    with pytest.raises(OmniAPIError) as caught:
        client._request("POST", f"/api/v1/models/{PRIVATE}/yaml", retry_transient=False)
    detail = assert_private_free(caught.value)
    assert detail["max_attempts"] == detail["attempt_count"] == session.request.call_count == 1
    assert detail["retryable"] is False


def test_response_size_and_pagination_failures_are_distinct():
    client, _ = client_for(response=Response(headers={"Content-Length": str(65 * 1024 * 1024)}))
    with pytest.raises(OmniAPIError) as caught:
        client.get_model_yaml(PRIVATE)
    assert assert_private_free(caught.value)["reason_code"] == "omni_response_limit_exceeded"
    client, _ = client_for(response=Response(body=b'{"records":[],"pageInfo":{"hasNextPage":true}}'))
    with pytest.raises(OmniAPIError) as caught:
        client.list_models()
    assert assert_private_free(caught.value)["reason_code"] == "omni_pagination_invalid"


@pytest.mark.parametrize("payload", [
    {}, [None], [{"message": {"private": PRIVATE}}], [{"message": "safe", "is_warning": "yes"}],
    [{"message": "safe", "yaml_path": []}], [{"message": "safe", "auto_fix": []}],
    [{"message": "safe", "auto_fix": {"description_short": []}}],
])
def test_client_model_normalization_retains_structural_failure_code(payload):
    client, _ = client_for(response=Response(body=json.dumps(payload).encode()))
    with pytest.raises(OmniAPIError) as caught:
        client.validate_model(PRIVATE)
    detail = exception_diagnostic(caught.value)
    assert detail["reason_code"] == "invalid_model_validation_response"
    assert detail["api_operation"] == "model_validation"
    assert PRIVATE not in json.dumps(detail) + str(caught.value)


@pytest.mark.parametrize("status,reason,error_type", [
    (401, "github_unauthorized", GitHubAuthError),
    (403, "github_forbidden", GitHubAuthError),
    (404, "github_not_found_or_inaccessible", GitHubAPIError),
    (429, "github_rate_limited", GitHubAPIError),
    (503, "github_service_unavailable", GitHubAPIError),
])
def test_github_http_failures_are_not_labeled_readiness(monkeypatch, status, reason, error_type):
    request = Mock(return_value=Response(status, PRIVATE.encode()))
    monkeypatch.setattr("omniflow.github.revalidation.requests.request", request)
    api = GitHubRepository("synthetic/repo", "synthetic-token")
    with pytest.raises(error_type) as caught:
        api.request("GET", "/git/trees/" + "a" * 40)
    detail = assert_private_free(caught.value)
    assert detail["reason_code"] == reason
    assert detail["api_operation"] == "github_tree"
    assert detail["http_status"] == status
    assert request.call_count == detail["attempt_count"] == detail["max_attempts"] == 1
    assert "readiness" not in str(caught.value)


@pytest.mark.parametrize("response,error,reason", [
    (None, requests.Timeout(PRIVATE), "github_timeout"),
    (None, requests.exceptions.SSLError(PRIVATE), "github_tls_error"),
    (None, requests.ConnectionError(PRIVATE), "github_connection_error"),
    (Response(body=b"not json"), None, "github_invalid_json_response"),
    (Response(body=b'{"x":1,"x":2}'), None, "github_invalid_json_response"),
    (Response(stream_error=requests.Timeout(PRIVATE)), None, "github_response_interrupted"),
])
def test_github_transport_categories(monkeypatch, response, error, reason):
    monkeypatch.setattr("omniflow.github.revalidation.requests.request", Mock(return_value=response, side_effect=error))
    with pytest.raises(GitHubAPIError) as caught:
        GitHubRepository("synthetic/repo", "synthetic-token").request("GET", "/compare/" + "a" * 40)
    assert assert_private_free(caught.value)["reason_code"] == reason


def test_github_missing_token_and_response_limit_keep_configuration_and_security_types(monkeypatch):
    with pytest.raises(ConfigError) as caught:
        GitHubRepository("synthetic/repo", "")
    assert assert_private_free(caught.value)["reason_code"] == "github_missing_token"
    response = Response(body=b"too long")
    monkeypatch.setattr("omniflow.github.revalidation.requests.request", Mock(return_value=response))
    with pytest.raises(SecurityPolicyError) as caught:
        GitHubRepository("synthetic/repo", "synthetic-token").request("GET", "/git/blobs/" + "a" * 40, max_bytes=1)
    assert assert_private_free(caught.value)["reason_code"] == "github_response_limit_exceeded"


def test_github_rate_limit_403_is_not_confused_with_missing_permission(monkeypatch):
    response = Response(403, headers={"X-RateLimit-Remaining": "0"})
    monkeypatch.setattr("omniflow.github.revalidation.requests.request", Mock(return_value=response))
    with pytest.raises(GitHubAPIError) as caught:
        GitHubRepository("synthetic/repo", "synthetic-token").request("GET", "/git/trees/" + "a" * 40)
    assert assert_private_free(caught.value)["reason_code"] == "github_rate_limited"


def candidate_fixture(monkeypatch, git_files, api_files):
    sha = "a" * 40
    monkeypatch.setattr(candidate, "pull_request_revision", lambda _: (sha, "synthetic/repo"))
    entries = []
    for path, value in git_files.items():
        raw = value.encode()
        entries.append({"path": f"omni/{path}", "size": len(raw), "mode": "100644", "type": "blob",
                        "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()})
    monkeypatch.setattr(candidate, "_local_tree", lambda *_, **__: entries)
    reader = Mock(side_effect=lambda _, path, **__: git_files[path.removeprefix("omni/")])
    monkeypatch.setattr(candidate, "read_git_text", reader)
    client = SimpleNamespace(get_model_yaml=Mock(return_value={"files": api_files}))
    context = SimpleNamespace(model_path="omni", model_id="synthetic-model", branch_id="synthetic-branch")
    return client, context, reader


@pytest.mark.parametrize("side", ["head", "base"])
def test_candidate_case_hints_keep_original_paths_without_claiming_correspondence(monkeypatch, side):
    files = {f"old folder/schema{index}/orders{index}.view.yaml": "same" for index in range(7)}
    api = {f"new folder/schema{index}/Orders{index}.view": "same" for index in range(7)}
    client, context, reader = candidate_fixture(monkeypatch, files, api)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(client, context, revision_side=side)
    error = caught.value
    assert error.candidate_reason == "file_inventory_mismatch"
    assert error.contents_comparison == "not_compared"
    assert error.candidate_counts["case_only_count"] == 7
    assert error.candidate_counts["directory_only_count"] == 0
    assert error.candidate_counts["candidate_sample_count"] == 5
    assert error.candidate_counts["candidate_sample_omitted_count"] == 2
    assert error.candidate_counts["compared_file_count"] == 0
    assert error.candidate_git_sha == "a" * 40
    assert error.candidate_model_id == context.model_id
    assert error.candidate_branch_id == (context.branch_id if side == "head" else None)
    for sample in error.candidate_samples:
        assert sample["git_path"] in files and sample["omni_path"] in api
        assert sample["comparison"] == "not_compared" and sample["pairing"] == "case_hint"
        assert sample["validation_scope"] == ("branch" if side == "head" else "base")
    # Qualification reads and hash-verifies immutable Git blobs, but a case
    # hint still authorizes neither correspondence nor a byte comparison.
    assert reader.call_count == len(files)
    assert {(call.args[0], call.args[1]) for call in reader.call_args_list} == {
        ("a" * 40, "omni/" + path) for path in files
    }
    assert "old folder" not in str(error)


def test_candidate_content_failure_reports_only_observed_mismatch(monkeypatch):
    client, context, _ = candidate_fixture(monkeypatch, {"orders.view.yaml": "old"}, {"orders.view": "changed"})
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(client, context)
    error = caught.value
    assert error.contents_comparison == "different"
    assert error.candidate_counts["compared_file_count"] == 1
    assert error.candidate_samples == [{"git_path": "orders.view.yaml", "omni_path": "orders.view",
                                       "comparison": "different", "pairing": "authored_identity", "validation_scope": "branch"}]
    assert "changed" not in json.dumps(error.candidate_samples)


@pytest.mark.parametrize("file_count", [2, 4])
def test_candidate_ambiguous_basenames_block_with_bounded_unpaired_private_samples(monkeypatch, file_count):
    files = {f"old{index}/orders.view.yaml": "same" for index in range(file_count)}
    api = {f"new{index}/orders.view": "same" for index in range(file_count)}
    client, context, reader = candidate_fixture(
        monkeypatch, files, api,
    )
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(client, context)
    error = caught.value
    assert error.candidate_reason == "ambiguous_file_identity"
    assert error.contents_comparison == "not_compared"
    assert error.candidate_counts["compared_file_count"] == 0
    assert error.candidate_counts["candidate_sample_count"] == min(5, file_count * 2)
    assert error.candidate_counts["candidate_sample_omitted_count"] == max(0, file_count * 2 - 5)
    for sample in error.candidate_samples:
        assert (sample["git_path"] is None) != (sample["omni_path"] is None)
        assert sample["git_path"] is None or sample["git_path"] in files
        assert sample["omni_path"] is None or sample["omni_path"] in api
        assert sample["comparison"] == "not_compared" and sample["pairing"] == "ambiguous_identity"
        assert sample["validation_scope"] == "branch"
    assert reader.call_count == file_count
    assert {call.args[1] for call in reader.call_args_list} == {"omni/" + path for path in files}
    safe = public_diagnostic({"exit_code": error.exit_code, "issues": [{
        "validator": "candidate", "type": "candidate_verification_failed", "severity": "error",
        "candidate_reason": error.candidate_reason, "candidate_samples": error.candidate_samples,
    }]})
    assert safe["exit_code"] == 2 and "candidate_samples" not in safe["issues"][0]
    assert all(path not in json.dumps(safe) + str(error) for path in (*files, *api))


@pytest.mark.parametrize("error", [
    OmniAuthError(PRIVATE, reason_code="omni_forbidden"),
    OmniAPIError(PRIVATE, reason_code="omni_timeout"),
    OmniResponseError("invalid_json_response"),
    SecurityPolicyError(PRIVATE),
])
def test_candidate_keeps_upstream_error_type_and_scope(monkeypatch, error):
    client, context, _ = candidate_fixture(monkeypatch, {"model.yaml": "{}"}, {"model": "{}"})
    client.get_model_yaml.side_effect = error
    with pytest.raises(type(error)) as caught:
        verify_candidate(client, context, revision_side="base")
    assert caught.value is error
    assert error.candidate_reason == "api_snapshot_unavailable"
    assert error.validation_scope == "base"
    if hasattr(error, "reason_code"):
        assert exception_diagnostic(error)["cause_code"] == error.reason_code


@pytest.mark.parametrize("error", [
    GitHubAuthError(PRIVATE, reason_code="github_forbidden"),
    GitHubAPIError(PRIVATE, reason_code="github_timeout"),
    SecurityPolicyError(PRIVATE, reason_code="github_response_limit_exceeded"),
])
def test_candidate_remote_failure_keeps_type_and_local_git_cause(monkeypatch, error):
    client, context, _ = candidate_fixture(monkeypatch, {"model.yaml": "{}"}, {"model": "{}"})
    monkeypatch.setenv("OMNIFLOW_GITHUB_TOKEN", "synthetic-token")
    monkeypatch.setattr(candidate, "_local_tree", Mock(side_effect=candidate._GitUnavailable("git_timeout")))
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository.request", Mock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        verify_candidate(client, context)
    assert caught.value is error
    assert error.candidate_reason == "git_snapshot_unavailable"
    detail = assert_private_free(error)
    assert detail["local_git_reason"] == "git_timeout"
    assert detail["cause_code"] == error.reason_code


@pytest.mark.parametrize("error,reason", [
    (subprocess.TimeoutExpired("git", 10), "git_timeout"),
    (subprocess.CalledProcessError(1, "git"), "git_revision_unavailable"),
    (UnicodeDecodeError("utf8", b"x", 0, 1, "synthetic"), "git_invalid_encoding"),
])
def test_git_revision_failure_has_safe_cause(monkeypatch, tmp_path, error, reason):
    monkeypatch.setattr(revision_data, "git_executable", lambda: "/synthetic/git")
    monkeypatch.setattr(revision_data.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(ConfigError) as caught:
        revision_data.read_git_text("a" * 40, "model.yaml", root=tmp_path, max_bytes=100)
    assert assert_private_free(caught.value)["reason_code"] == reason
