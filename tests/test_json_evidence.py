from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from omniflow.exceptions import ConfigError, OmniResponseError
from omniflow.omni_client import OmniClient


def client_for(body):
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response._content_consumed = True
    session = SimpleNamespace(headers={}, request=Mock(return_value=response))
    return OmniClient(base_url="https://synthetic.example", api_key="synthetic", session=session), session


@pytest.mark.parametrize("body", [
    b'{"content":[{"document_id":"PRIVATE"}],"content":[]}',
    b'{"content":[{"issues":["PRIVATE"],"issues":[]}]}',
])
def test_duplicate_json_never_erases_evidence(body):
    client, _ = client_for(body)
    with pytest.raises(OmniResponseError) as caught:
        client.validate_content("model")
    assert caught.value.exit_code == 4
    assert caught.value.reason_code == "invalid_json_response"
    assert "PRIVATE" not in str(caught.value)


def test_repeated_field_names_in_separate_objects_remain_valid():
    client, _ = client_for(b'{"content":[{"name":"a"},{"name":"b"}]}')
    assert len(client.validate_content("model")["content"]) == 2


@pytest.mark.parametrize("full", [False, True])
def test_full_depth_is_explicit_and_does_not_change_visibility(full):
    client, session = client_for(b'{"content":[]}')
    client.validate_content("model", branch_id="branch", user_id="user",
                            include_personal_folders=True, force_full_validation=full)
    params = session.request.call_args.kwargs["params"]
    assert params["branch_id"] == "branch"
    assert params["userId"] == "user"
    assert params["include_personal_folders"] == "true"
    assert params.get("force_full_validation") == ("true" if full else None)


def test_full_depth_does_not_accept_truthy_strings():
    client, session = client_for(b'{"content":[]}')
    with pytest.raises(ConfigError):
        client.validate_content("model", force_full_validation="false")
    session.request.assert_not_called()
