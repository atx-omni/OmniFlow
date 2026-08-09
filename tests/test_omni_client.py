import unittest
from unittest import mock

from omniflow.exceptions import ConfigError, OmniAPIError
from omniflow.omni_client import MAX_RESPONSE_BYTES, OmniClient

FAKE_API_KEY = "secret"  # pragma: allowlist secret


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300
        self.text = "text"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class InvalidJsonResponse(FakeResponse):
    def json(self):
        raise ValueError("private response fragment")


class StreamingResponse(FakeResponse):
    def __init__(self, chunks):
        super().__init__(None)
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        yield from self.chunks

    def close(self):
        self.closed = True


class OmniClientTests(unittest.TestCase):
    def test_branch_resolution(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "records": [
                            {"id": "branch-1", "modelKind": "BRANCH", "baseModelId": "model-1", "name": "feature/a"}
                        ],
                        "pageInfo": {},
                    }
                )
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(client.resolve_branch_id("model-1", "feature/a"), "branch-1")
        self.assertTrue(session.calls[0][2]["stream"])

    def test_retries_429(self):
        session = FakeSession([FakeResponse({}, status_code=429), FakeResponse([], status_code=200)])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(client.validate_model("model-1"), [])
        self.assertEqual(len(session.calls), 2)

    def test_error_response_does_not_echo_server_payload(self):
        response = FakeResponse({}, status_code=400)
        response.text = "sensitive customer payload"
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([response]),
        )
        with self.assertRaises(OmniAPIError) as raised:
            client.validate_model("model-1")
        self.assertNotIn("sensitive customer payload", str(raised.exception))

    def test_rejects_oversized_and_invalid_json_responses_without_echoing_body(self):
        oversized = FakeResponse([], headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([oversized]),
        )
        with self.assertRaises(OmniAPIError):
            client.validate_model("model-1")

        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([InvalidJsonResponse(None)]),
        )
        with self.assertRaises(OmniAPIError) as raised:
            client.validate_model("model-1")
        self.assertNotIn("private response fragment", str(raised.exception))

    def test_streamed_response_limit_closes_connection(self):
        response = StreamingResponse([b"123", b"45"])
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([response]),
        )
        with mock.patch("omniflow.omni_client.MAX_RESPONSE_BYTES", 4):
            with self.assertRaises(OmniAPIError):
                client.validate_model("model-1")
        self.assertTrue(response.closed)

    def test_pagination_rejects_repeated_cursor_and_record_overflow(self):
        repeated = FakeSession(
            [
                FakeResponse({"records": [], "pageInfo": {"nextCursor": "same"}}),
                FakeResponse({"records": [], "pageInfo": {"nextCursor": "same"}}),
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=repeated)
        with self.assertRaises(OmniAPIError):
            client.list_models()

        overflow = FakeSession([FakeResponse({"records": [{"id": "a"}, {"id": "b"}], "pageInfo": {}})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=overflow)
        with mock.patch("omniflow.omni_client.MAX_PAGINATION_RECORDS", 1):
            with self.assertRaises(OmniAPIError):
                client.list_models()

        malformed = FakeSession([FakeResponse({"records": ["bad", "records"], "pageInfo": {}})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=malformed)
        with mock.patch("omniflow.omni_client.MAX_PAGINATION_RECORDS", 1):
            with self.assertRaises(OmniAPIError):
                client.list_models()

    def test_rejects_invalid_timeout_and_find_type(self):
        with self.assertRaises(ConfigError):
            OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, timeout=0)
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([]),
        )
        with self.assertRaises(ConfigError):
            client.search_content_references("model-1", find="orders", find_type="relationship")

    def test_content_validator_find_type_uses_documented_enum_values(self):
        session = FakeSession([FakeResponse({"content": []})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        client.search_content_references("model-1", find="orders.revenue", find_type="field")
        _, _, kwargs = session.calls[0]
        self.assertEqual(kwargs["params"]["find"], "orders.revenue")
        self.assertEqual(kwargs["params"]["find_type"], "FIELD")

    def test_get_dbt_exposures_uses_documented_endpoint(self):
        session = FakeSession([FakeResponse({"records": [], "pageInfo": {"hasNextPage": False}})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(client.get_dbt_exposures("model-1", branch_id="branch-1"), {"records": []})
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://omni.example/api/v1/models/model-1/dbt-exposures")
        self.assertEqual(kwargs["params"]["branch_id"], "branch-1")

    def test_content_metadata_uses_documented_organization_scope(self):
        session = FakeSession([FakeResponse({"records": [], "pageInfo": {}})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        client.list_content(labels=["Verified"], branch_id="not-supported-by-content-api")
        _, _, kwargs = session.calls[0]
        self.assertEqual(
            kwargs["params"],
            {"include": "labels", "scope": "organization", "labels": "Verified", "pageSize": 100},
        )

    def test_personal_content_metadata_uses_restricted_scope(self):
        session = FakeSession(
            [
                FakeResponse({"records": [{"id": "org"}], "pageInfo": {}}),
                FakeResponse({"records": [{"id": "personal"}], "pageInfo": {}}),
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        records = client.list_content(include_personal_folders=True, user_id="user-1")
        self.assertEqual([record["id"] for record in records], ["org", "personal"])
        self.assertEqual(session.calls[1][2]["params"]["scope"], "restricted")
        self.assertEqual(session.calls[1][2]["params"]["creatorId"], "user-1")

    def test_label_filtering_personal_content_requires_user_id(self):
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([]),
        )
        with self.assertRaises(ConfigError):
            client.list_content(labels=["Verified"], include_personal_folders=True)


if __name__ == "__main__":
    unittest.main()
