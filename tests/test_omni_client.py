import unittest

from omniflow.omni_client import OmniClient


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
        client = OmniClient(base_url="https://omni.example", api_key="secret", session=session)
        self.assertEqual(client.resolve_branch_id("model-1", "feature/a"), "branch-1")

    def test_retries_429(self):
        session = FakeSession([FakeResponse({}, status_code=429), FakeResponse([], status_code=200)])
        client = OmniClient(base_url="https://omni.example", api_key="secret", session=session)
        self.assertEqual(client.validate_model("model-1"), [])
        self.assertEqual(len(session.calls), 2)

    def test_content_validator_find_type_uses_documented_enum_values(self):
        session = FakeSession([FakeResponse({"content": []})])
        client = OmniClient(base_url="https://omni.example", api_key="secret", session=session)
        client.search_content_references("model-1", find="orders.revenue", find_type="field")
        _, _, kwargs = session.calls[0]
        self.assertEqual(kwargs["params"]["find"], "orders.revenue")
        self.assertEqual(kwargs["params"]["find_type"], "FIELD")

    def test_get_dbt_exposures_uses_documented_endpoint(self):
        session = FakeSession([FakeResponse({"exposures": []})])
        client = OmniClient(base_url="https://omni.example", api_key="secret", session=session)
        self.assertEqual(client.get_dbt_exposures("model-1"), {"exposures": []})
        _, url, _ = session.calls[0]
        self.assertEqual(url, "https://omni.example/api/v1/models/model-1/dbt-exposures")


if __name__ == "__main__":
    unittest.main()
