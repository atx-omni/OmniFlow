import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.config import ContractSettings
from omniflow.contracts import evaluate_contracts
from omniflow.discovery import (
    discover_contexts,
    load_flow_metadata,
    load_pr_marker,
)
from omniflow.downstream import generate_downstream_dependencies
from omniflow.exceptions import OmniAPIError
from omniflow.exceptions import ConfigError, SecurityPolicyError


@contextlib.contextmanager
def temporary_workdir():
    original = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(original)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class DiscoveryTests(unittest.TestCase):
    def test_single_model_auto_discovery_works_without_policy_config(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                            "base_branch": "main",
                        }
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"GITHUB_HEAD_REF": "feature/a"}, clear=True):
                contexts = discover_contexts(auto=True)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].model_id, "model-1")
        self.assertEqual(contexts[0].branch_name, "feature/a")

    def test_auto_routing_skips_when_no_omni_context_exists(self):
        with temporary_workdir():
            with mock.patch.dict(os.environ, {}, clear=True):
                contexts = discover_contexts(auto=True, allow_skip=True)
        self.assertEqual(contexts, [])

    def test_auto_routing_skips_non_omni_changed_files(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                        }
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"OMNIFLOW_CHANGED_FILES": "models/marts/fact_orders.sql"}, clear=True):
                contexts = discover_contexts(auto=True, allow_skip=True)
        self.assertEqual(contexts, [])

    def test_multi_model_selection_by_changed_file_prefix(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                        {"base_url": "https://omni.example", "model_id": "b", "model_path": "omni/b"},
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"OMNIFLOW_CHANGED_FILES": "omni/b/views/orders.view"}, clear=True):
                contexts = discover_contexts(auto=True)
        self.assertEqual([context.model_id for context in contexts], ["b"])

    def test_multiple_changed_model_paths_run_multiple_contexts(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                        {"base_url": "https://omni.example", "model_id": "b", "model_path": "omni/b"},
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"OMNIFLOW_CHANGED_FILES": "omni/a/model.yaml\nomni/b/model.yaml"}, clear=True):
                contexts = discover_contexts(auto=True)
        self.assertEqual([context.model_id for context in contexts], ["a", "b"])

    def test_pr_marker_resolves_ambiguous_content_only_pr(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                        {"base_url": "https://omni.example", "model_id": "b", "model_path": "omni/b"},
                    ],
                },
            )
            event = {
                "pull_request": {
                    "body": '<!-- omniflow-context {"model_id":"b","branch_name":"feature/content"} -->'
                }
            }
            write_json(tmp / "event.json", event)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                marker = load_pr_marker()
                contexts = discover_contexts(auto=True)
        self.assertEqual(marker["model_id"], "b")
        self.assertEqual(contexts[0].model_id, "b")
        self.assertEqual(contexts[0].branch_name, "feature/content")

    def test_pr_marker_cannot_provide_base_url_without_trusted_source(self):
        with temporary_workdir() as tmp:
            event = {
                "pull_request": {
                    "body": '<!-- omniflow-context {"base_url":"https://omni.example","model_id":"model-1","model_path":"omni/model","branch_name":"feature/a"} -->'
                }
            }
            write_json(tmp / "event.json", event)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                with self.assertRaises(SecurityPolicyError):
                    discover_contexts(auto=True)

    def test_pr_marker_uses_trusted_base_url_override_without_flow_file(self):
        with temporary_workdir() as tmp:
            event = {
                "pull_request": {
                    "body": '<!-- omniflow-context {"model_id":"model-1","model_path":"omni/model","branch_name":"feature/a"} -->'
                }
            }
            write_json(tmp / "event.json", event)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                contexts = discover_contexts(auto=True, base_url="https://omni.example")
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].base_url, "https://omni.example")
        self.assertEqual(contexts[0].model_id, "model-1")
        self.assertEqual(contexts[0].model_path, "omni/model")

    def test_auto_routing_skips_single_model_when_changed_files_unavailable(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"GITHUB_HEAD_REF": "feature/dbt"}, clear=True):
                contexts = discover_contexts(auto=True, allow_skip=True)
        self.assertEqual(contexts, [])

    def test_malformed_pr_marker_is_config_error(self):
        with temporary_workdir() as tmp:
            event = {"pull_request": {"body": '<!-- omniflow-context {"model_id":} -->'}}
            write_json(tmp / "event.json", event)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                with self.assertRaises(ConfigError):
                    load_pr_marker()

    def test_ambiguous_multi_model_without_marker_fails(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                        {"base_url": "https://omni.example", "model_id": "b", "model_path": "omni/b"},
                    ],
                },
            )
            with self.assertRaises(ConfigError):
                discover_contexts(auto=True)

    def test_metadata_rejects_secret_keys(self):
        with temporary_workdir() as tmp:
            write_json(tmp / ".omni/flow.json", {"version": 1, "api_key": "bad", "models": []})
            with self.assertRaises(SecurityPolicyError):
                load_flow_metadata()


class ContractImpactTests(unittest.TestCase):
    def dependencies(self):
        return {
            "version": 1,
            "model_id": "model-1",
            "dependencies": [
                {
                    "content_id": "dash-1",
                    "content_type": "dashboard",
                    "content_name": "Executive Revenue",
                    "labels": ["Verified"],
                    "query_id": "query-1",
                    "query_name": "Revenue",
                    "references": [{"type": "field", "name": "orders.revenue"}],
                }
            ],
        }

    def test_deleted_referenced_field_fails(self):
        report, exit_code = evaluate_contracts(
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.revenue", "risk": "breaking"}]},
            dependencies=self.dependencies(),
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["issues"][0]["impact_level"], "referenced_breaking")

    def test_deleted_unreferenced_field_reports_without_failure(self):
        report, exit_code = evaluate_contracts(
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.margin", "risk": "breaking"}]},
            dependencies=self.dependencies(),
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["issues"][0]["impact_level"], "unreferenced")

    def test_referenced_type_and_cardinality_changes_fail(self):
        for change in (
            {"type": "field_type_changed", "field": "orders.revenue", "risk": "breaking"},
            {"type": "relationship_cardinality_changed", "name": "orders.revenue", "risk": "warning"},
        ):
            with self.subTest(change=change):
                _, exit_code = evaluate_contracts(
                    diff_result={"changes": [change]},
                    dependencies=self.dependencies(),
                    settings=ContractSettings(),
                    model_id="model-1",
                )
                self.assertEqual(exit_code, 1)

    def test_advisory_referenced_change_reports_without_failure(self):
        report, exit_code = evaluate_contracts(
            diff_result={"changes": [{"type": "field_modified", "field": "orders.revenue", "risk": "warning"}]},
            dependencies=self.dependencies(),
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["issues"][0]["impact_level"], "referenced_safe")

    def test_reference_type_mismatch_does_not_match_contract_impact(self):
        report, exit_code = evaluate_contracts(
            diff_result={"changes": [{"type": "field_deleted", "field": "orders", "risk": "breaking"}]},
            dependencies={
                "dependencies": [
                    {
                        "content_id": "dash-1",
                        "references": [{"type": "topic", "name": "orders"}],
                    }
                ]
            },
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["issues"][0]["impact_level"], "unreferenced")

    def test_contract_report_includes_dependency_coverage(self):
        dependencies = self.dependencies()
        dependencies["generation_mode"] = "targeted_partial"
        dependencies["coverage_gaps"] = [{"type": "field", "name": "orders.margin", "message": "unavailable"}]
        report, _ = evaluate_contracts(
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.revenue", "risk": "breaking"}]},
            dependencies=dependencies,
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(report["dependency_generation_mode"], "targeted_partial")
        self.assertEqual(report["summary"]["dependency_coverage_gaps"], 1)


class FakeDependencyClient:
    def __init__(self, *, fail_targeted=False, fail_after_first=False):
        self.fail_targeted = fail_targeted
        self.fail_after_first = fail_after_first
        self.searches = []

    def search_content_references(self, model_id, *, find, find_type, **kwargs):
        self.searches.append((find_type, find))
        if self.fail_targeted or (self.fail_after_first and len(self.searches) > 1):
            raise OmniAPIError("targeted search unavailable")
        return {
            "content": [
                {
                    "document_id": "dash-1",
                    "identifier": "dashboard-1",
                    "type": "dashboard",
                    "name": "Executive Revenue",
                    "owner": {"name": "Jane Doe", "email": "jane@example.com"},
                    "labels": [{"name": "Verified"}],
                    "queries_and_issues": [
                        {"query_presentation_id": "query-1", "query_name": "Revenue by Month"}
                    ],
                }
            ]
        }

    def validate_content(self, model_id, **kwargs):
        return {
            "content": [
                {
                    "document_id": "dash-fallback",
                    "type": "dashboard",
                    "name": "Fallback Dashboard",
                    "queries_and_issues": [{"query_presentation_id": "query-x", "query_name": "Fallback"}],
                }
            ]
        }


class DownstreamGenerationTests(unittest.TestCase):
    def test_generates_dependencies_from_targeted_content_search(self):
        client = FakeDependencyClient()
        dependency_graph = generate_downstream_dependencies(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.revenue"}]},
        )
        self.assertEqual(client.searches, [("field", "orders.revenue")])
        self.assertEqual(dependency_graph["generation_mode"], "targeted")
        self.assertEqual(dependency_graph["dependencies"][0]["content_id"], "dash-1")
        self.assertEqual(dependency_graph["dependencies"][0]["references"], [{"type": "field", "name": "orders.revenue"}])

    def test_falls_back_to_full_validation_when_targeted_search_unavailable(self):
        dependency_graph = generate_downstream_dependencies(
            client=FakeDependencyClient(fail_targeted=True),
            model_id="model-1",
            branch_id="branch-1",
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.revenue"}]},
        )
        self.assertEqual(dependency_graph["generation_mode"], "full_validation_fallback")
        self.assertEqual(dependency_graph["dependencies"][0]["content_id"], "dash-fallback")

    def test_partial_targeted_failure_preserves_successful_dependencies(self):
        dependency_graph = generate_downstream_dependencies(
            client=FakeDependencyClient(fail_after_first=True),
            model_id="model-1",
            branch_id="branch-1",
            diff_result={
                "changes": [
                    {"type": "field_deleted", "field": "orders.revenue"},
                    {"type": "field_deleted", "field": "orders.margin"},
                ]
            },
        )
        self.assertEqual(dependency_graph["generation_mode"], "targeted_partial")
        self.assertEqual(dependency_graph["dependencies"][0]["content_id"], "dash-1")
        self.assertEqual(dependency_graph["coverage_gaps"][0]["name"], "orders.margin")


if __name__ == "__main__":
    unittest.main()
