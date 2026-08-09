import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.config import load_config
from omniflow.exceptions import SecurityPolicyError
from omniflow.security import public_safe, redact


class ConfigSecurityTests(unittest.TestCase):
    def test_config_precedence_and_env_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text(
                """
omni:
  base_url: https://omni.example
  model_id: model-1
  branch_name: ${GITHUB_HEAD_REF}
checks:
  content_validation:
    labels: [Verified]
contracts:
  fail_on:
    deleted_referenced_fields: false
""",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"GITHUB_HEAD_REF": "feature/a", "OMNI_MODEL_ID": "model-env"}, clear=True):
                config = load_config(path)
        self.assertEqual(config.omni.model_id, "model-env")
        self.assertEqual(config.omni.branch_name, "feature/a")
        self.assertEqual(config.content_validation.labels, ["Verified"])
        self.assertFalse(config.contracts.fail_on_deleted_referenced_fields)

    def test_policy_config_can_be_empty_for_auto_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text("checks:\n  model_validation:\n    enabled: true\n", encoding="utf-8")
            config = load_config(path)
        self.assertIsNone(config.omni.base_url)
        self.assertIsNone(config.omni.model_id)
        self.assertTrue(config.model_validation.enabled)

    def test_rejects_secret_like_config_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text("omni:\n  api_key: nope\n", encoding="utf-8")
            with self.assertRaises(SecurityPolicyError):
                load_config(path)

    def test_redacts_tokens_from_strings_and_mappings(self):
        self.assertEqual(redact("Authorization: Bearer abc123"), "Authorization: Bearer [REDACTED]")
        self.assertEqual(redact({"nested": {"password": "abc"}}), {"nested": {"password": "[REDACTED]"}})

    def test_security_artifact_options_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text(
                """
security:
  redaction_level: strict
  retain_restricted_artifacts: false
checks:
  dbt_exposures:
    enabled: true
    fail_on_unavailable: true
""",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config.security.redaction_level, "strict")
        self.assertFalse(config.security.retain_restricted_artifacts)
        self.assertTrue(config.dbt_exposures.enabled)
        self.assertTrue(config.dbt_exposures.fail_on_unavailable)

    def test_public_safe_standard_removes_emails_urls_and_raw_payloads(self):
        payload = {
            "content_name": "Executive Revenue",
            "owner": {"name": "Alice", "email": "alice@example.com"},
            "content_url": "https://omni.example/dash",
            "raw": {"anything": "goes"},
        }
        safe = public_safe(payload, redaction_level="standard")
        self.assertEqual(safe["content_name"], "Executive Revenue")
        self.assertEqual(safe["owner"]["name"], "Alice")
        self.assertEqual(safe["owner"]["email"], "[REDACTED]")
        self.assertEqual(safe["content_url"], "[REDACTED]")
        self.assertNotIn("raw", safe)

    def test_public_safe_strict_redacts_names_and_owner_metadata(self):
        payload = {
            "content_name": "Executive Revenue",
            "owner": {"name": "Alice", "email": "alice@example.com"},
            "labels": ["Executive"],
        }
        safe = public_safe(payload, redaction_level="strict")
        self.assertEqual(safe["content_name"], "[REDACTED]")
        self.assertEqual(safe["owner"], "[REDACTED]")
        self.assertEqual(safe["labels"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
