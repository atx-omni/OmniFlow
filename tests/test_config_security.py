import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.config import load_config, require_repair_api_key
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.security import public_safe, redact


class ConfigSecurityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "symbolic links require POSIX support")
    def test_rejects_symbolic_link_policy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "policy.yml"
            target.write_text("checks: {}\n", encoding="utf-8")
            link = root / ".omniflow.yml"
            link.symlink_to(target)
            with self.assertRaises(ConfigError):
                load_config(link)

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
            with mock.patch.dict(
                os.environ, {"GITHUB_HEAD_REF": "feature/a", "OMNI_MODEL_ID": "model-env"}, clear=True
            ):
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
        self.assertFalse(config.security.retain_restricted_artifacts)
        self.assertFalse(config.ai_repair.enabled)
        self.assertFalse(config.ai_repair.allow_query_execution)

    def test_ai_repair_requires_explicit_opt_in_and_bounded_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text(
                """
repairs:
  ai:
    enabled: true
    allow_query_execution: true
    max_changed_files: 4
    max_changed_lines: 300
    poll_timeout_seconds: 420
""",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertTrue(config.ai_repair.enabled)
        self.assertTrue(config.ai_repair.allow_query_execution)
        self.assertEqual(config.ai_repair.max_changed_files, 4)
        self.assertEqual(config.ai_repair.max_changed_lines, 300)
        self.assertEqual(config.ai_repair.poll_timeout_seconds, 420)

    def test_ai_repair_rejects_unknown_keys_and_unbounded_limits(self):
        for body in (
            "repairs:\n  ai:\n    prompt: fix everything\n",
            "repairs:\n  ai:\n    max_changed_files: 0\n",
            "repairs:\n  ai:\n    max_changed_lines: 2001\n",
            "repairs:\n  ai:\n    poll_timeout_seconds: 29\n",
        ):
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / ".omniflow.yml"
                    path.write_text(body, encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(path)

    def test_repair_key_is_read_only_from_dedicated_environment_variable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError):
                require_repair_api_key()
        with mock.patch.dict(
            os.environ,
            {"OMNIFLOW_REPAIR_API_KEY": "repair-token"},  # pragma: allowlist secret
            clear=True,
        ):
            self.assertEqual(require_repair_api_key(), "repair-token")

    def test_rejects_unknown_top_level_and_nested_policy_keys(self):
        for body in (
            "unknown_section: {}\n",
            "security:\n  redact_log: true\n",
            "checks:\n  model_validation:\n    fail_on_warning: true\n",
        ):
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / ".omniflow.yml"
                    path.write_text(body, encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(path)

    def test_rejects_cyclic_yaml_alias_in_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text("security: &loop\n  child: *loop\n", encoding="utf-8")
            with self.assertRaises(SecurityPolicyError):
                load_config(path)

    def test_rejects_oversized_trusted_policy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text("#" + ("x" * (1024 * 1024)), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_secret_like_config_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text("omni:\n  api_key: nope\n", encoding="utf-8")
            with self.assertRaises(SecurityPolicyError):
                load_config(path)

    def test_rejects_secret_environment_expansion_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text("reporting:\n  output_dir: ${OMNI_API_KEY}\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"OMNI_API_KEY": "do-not-expand"},  # pragma: allowlist secret
                clear=True,
            ):
                with self.assertRaises(SecurityPolicyError):
                    load_config(path)

    def test_rejects_unsafe_raw_output_in_ci_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".omniflow.yml"
            path.write_text("security:\n  allow_raw_response_output: true\n", encoding="utf-8")
            with self.assertRaises(SecurityPolicyError):
                load_config(path)

    def test_rejects_output_directory_symlinked_outside_repository(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as outside:
            os.chdir(repo)
            try:
                Path(".omniflow").symlink_to(outside, target_is_directory=True)
                with self.assertRaises(SecurityPolicyError):
                    load_config()
            finally:
                os.chdir(original)

    def test_rejects_output_directory_symlinked_within_repository(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as repo:
            os.chdir(repo)
            try:
                Path("actual-output").mkdir()
                Path(".omniflow").symlink_to("actual-output", target_is_directory=True)
                with self.assertRaises(SecurityPolicyError):
                    load_config()
            finally:
                os.chdir(original)

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
