import json
import tempfile
import unittest
from pathlib import Path

from omniflow.exceptions import ConfigError
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph
from omniflow.diff.yaml_loader import load_yaml_files
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.sarif_report import to_sarif
from omniflow.validators.yaml_lint import has_error, lint_graph
from omniflow.yaml_pull import pull_yaml


class FakeYamlClient:
    def get_model_yaml(self, *args, **kwargs):
        return {
            "files": {"views/orders.view": "name: orders\nfields:\n  id:\n    primary_key: true\n"},
            "checksums": {"views/orders.view": "abc"},
        }


class DiffLintReportTests(unittest.TestCase):
    def test_loads_composite_topic_files_as_topics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "revenue.composite_topic"
            path.write_text("name: Revenue Composite\nbase_view: orders\n", encoding="utf-8")
            graph = build_graph(load_yaml_files(tmp))
        self.assertIn("Revenue Composite", graph.topics)

    def test_yaml_pull_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pull_yaml(
                client=FakeYamlClient(),
                model_id="model-1",
                branch_id="branch-1",
                output_dir=tmp,
            )
            self.assertTrue((Path(tmp) / "views/orders.view").exists())
            self.assertEqual(manifest["files"]["views/orders.view"]["checksum"], "abc")

    def test_yaml_pull_rejects_branch_id_for_non_combined_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                pull_yaml(
                    client=FakeYamlClient(),
                    model_id="model-1",
                    branch_id="branch-1",
                    output_dir=tmp,
                    mode="staged",
                )

    def test_semantic_diff_detects_deleted_field_and_type_change(self):
        base = build_graph({"views/orders.view": {"name": "orders", "fields": {"id": {"type": "number"}, "revenue": {"type": "number"}}}})
        head = build_graph({"views/orders.view": {"name": "orders", "fields": {"id": {"type": "string"}}}})
        report = diff_graphs(base, head)
        types = {change["type"] for change in report["changes"]}
        self.assertIn("field_deleted", types)
        self.assertIn("field_type_changed", types)
        self.assertEqual(report["risk_level"], "breaking")

    def test_semantic_diff_detects_relationship_type_change(self):
        base = build_graph({"relationships/order_items.relationships": {"relationships": {"orders": {"relationship_type": "many_to_one"}}}})
        head = build_graph({"relationships/order_items.relationships": {"relationships": {"orders": {"relationship_type": "one_to_many"}}}})
        report = diff_graphs(base, head)
        types = {change["type"] for change in report["changes"]}
        self.assertIn("relationship_cardinality_changed", types)

    def test_rule_severity_handling(self):
        graph = build_graph({"views/orders.view": {"name": "orders", "fields": {"revenue": {"type": "number"}}}})
        issues = lint_graph(graph, configured_rules={"require_primary_keys": "error"})
        self.assertTrue(has_error(issues))

    def test_sarif_and_junit_output(self):
        report = {"tool_version": "0.4.0", "issues": [{"rule_id": "x", "severity": "error", "file": "a.yml", "message": "bad"}]}
        sarif = to_sarif(report)
        junit = to_junit(report)
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertIn("<failure", junit)

    def test_markdown_report_is_reviewer_friendly_for_contract_failure(self):
        report = {
            "tool_version": "0.4.0",
            "generated_at": "2026-08-09T00:00:00Z",
            "git_sha": "abc",
            "git_branch": "feature/a",
            "config_hash": "hash",
            "policy_decision": "fail",
            "exit_code_reason": "validation failed",
            "models": [{"model_id": "model-1", "model_path": "omni/model", "branch_name": "feature/a"}],
            "summary": {"total_issues": 1, "errors": 1, "warnings": 0, "risk_level": "breaking"},
            "issues": [
                {
                    "validator": "contracts",
                    "severity": "error",
                    "impact_level": "referenced_breaking",
                    "field": "orders.revenue",
                    "referenced_content": [{"content_id": "dash-1"}],
                    "message": "Deleted referenced field.",
                }
            ],
            "model_reports": [
                {
                    "check_reports": [
                        {
                            "coverage_gaps": [
                                {"type": "field", "name": "orders.margin", "message": "targeted search unavailable"}
                            ]
                        }
                    ]
                }
            ],
        }
        markdown = render_markdown_report(report)
        self.assertIn("## Decision", markdown)
        self.assertIn("Fail: review blocking issues before merge.", markdown)
        self.assertIn("`model-1` path `omni/model` branch `feature/a`", markdown)
        self.assertIn("## Downstream Contract Impact", markdown)
        self.assertIn("referenced content: `1`", markdown)
        self.assertIn("orders.margin", markdown)
        self.assertIn("Resolve blocking validation", markdown)

    def test_markdown_report_guides_skipped_non_omni_prs(self):
        markdown = render_markdown_report(
            {
                "tool_version": "0.4.0",
                "policy_decision": "skipped",
                "exit_code_reason": "no Omni PR context or changed Omni model files detected",
                "summary": {},
                "issues": [],
            }
        )
        self.assertIn("Skipped: no Omni semantic-layer changes were detected.", markdown)
        self.assertIn("No reviewer action needed", markdown)


if __name__ == "__main__":
    unittest.main()
