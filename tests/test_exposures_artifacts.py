import json
import tempfile
import unittest
from pathlib import Path

from omniflow.artifacts import write_artifact_manifest, write_public_reports
from omniflow.config import DbtExposureSettings
from omniflow.exceptions import OmniAPIError
from omniflow.exposures import run_dbt_exposure_enrichment


class FakeExposureClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def get_dbt_exposures(self, model_id):
        if self.error:
            raise self.error
        return self.payload


class ExposureArtifactTests(unittest.TestCase):
    def test_dbt_exposure_enrichment_normalizes_records(self):
        report, exit_code = run_dbt_exposure_enrichment(
            client=FakeExposureClient(
                {
                    "exposures": [
                        {
                            "id": "dash-1",
                            "name": "Executive Revenue",
                            "url": "https://omni.example/dash",
                            "owner": {"name": "Alice", "email": "alice@example.com"},
                            "depends_on": [{"name": "model.orders"}],
                        }
                    ]
                }
            ),
            model_id="model-1",
            settings=DbtExposureSettings(enabled=True),
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["summary"]["total_exposures"], 1)
        self.assertEqual(report["exposures"][0]["depends_on"], ["model.orders"])

    def test_dbt_exposure_enrichment_degrades_to_warning_by_default(self):
        report, exit_code = run_dbt_exposure_enrichment(
            client=FakeExposureClient(error=OmniAPIError("missing permission")),
            model_id="model-1",
            settings=DbtExposureSettings(enabled=True),
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["summary"]["coverage_status"], "unavailable")
        self.assertEqual(report["issues"][0]["severity"], "warning")

    def test_public_reports_write_root_and_public_redacted_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = {
                "tool_version": "0.4.0",
                "policy_decision": "pass",
                "summary": {},
                "issues": [{"content_name": "Executive Revenue", "owner": {"email": "alice@example.com"}}],
            }
            write_public_reports(report, output_dir=tmp, formats=["json", "markdown"], redaction_level="strict")
            root_report = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
            public_report = json.loads((Path(tmp) / "public/report.json").read_text(encoding="utf-8"))
        self.assertEqual(root_report, public_report)
        self.assertEqual(root_report["issues"][0]["content_name"], "[REDACTED]")
        self.assertEqual(root_report["issues"][0]["owner"], "[REDACTED]")

    def test_artifact_manifest_documents_public_and_restricted_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_artifact_manifest(output_dir=tmp, restricted_artifacts_enabled=True, redaction_level="standard")
            manifest = json.loads((Path(tmp) / "artifact-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["public_dir"], "public")
        self.assertEqual(manifest["restricted_dir"], "restricted")
        self.assertTrue(manifest["restricted_artifacts_enabled"])


if __name__ == "__main__":
    unittest.main()
