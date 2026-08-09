import re
import tomllib
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PINNED_USE_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def nested_uses(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = []
        for key, item in value.items():
            if key == "uses" and isinstance(item, str):
                values.append(item)
            values.extend(nested_uses(item))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(nested_uses(item))
        return values
    return []


class RepositoryHardeningTests(unittest.TestCase):
    def test_distribution_name_does_not_collide_with_existing_pypi_project(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["name"], "omniflow-ci")
        self.assertEqual(pyproject["project"]["scripts"]["omniflow"], "omniflow.cli:main")

    def test_all_first_party_workflow_actions_are_pinned_by_sha(self):
        workflow_paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
        workflow_paths.append(ROOT / ".github/workflow-examples/omniflow.yml")
        for path in workflow_paths:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            for use in nested_uses(payload):
                if use == "atx-omni/omniflow@<pinned-commit-sha>":
                    continue
                self.assertRegex(use, PINNED_USE_RE, msg=f"Unpinned action in {path}: {use}")

    def test_composite_action_does_not_interpolate_inputs_inside_shell_scripts(self):
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
        scripts = [step.get("run", "") for step in action["runs"]["steps"]]
        self.assertFalse(any("${{ inputs." in script for script in scripts))
        self.assertTrue(any("--skip-reason" in script for script in scripts))

    def test_example_workflow_fetches_history_and_uploads_only_public_evidence(self):
        text = (ROOT / ".github/workflow-examples/omniflow.yml").read_text(encoding="utf-8")
        self.assertIn("fetch-depth: 0", text)
        self.assertIn("pull_request_target:", text)
        self.assertNotIn("ref: ${{ github.event.pull_request.head", text)
        self.assertIn("Route fork pull request without Omni secret", text)
        self.assertNotIn("skip-reason: Fork pull requests", text)
        self.assertIn(".omniflow/public/report.json", text)
        self.assertNotIn(".omniflow/restricted/", text)

    def test_legacy_package_and_packaging_shims_are_absent(self):
        self.assertFalse((ROOT / "setup.py").exists())
        self.assertFalse((ROOT / "setup.cfg").exists())
        self.assertFalse((ROOT / "src/omni_content_validator").exists())
        tracked_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and "tests" not in path.parts
            and "build" not in path.parts
            and "dist" not in path.parts
            and "egg-info" not in path.as_posix()
        )
        self.assertNotIn("omni-content-validator", tracked_text)
        self.assertNotIn(".omni-content-validator.yml", tracked_text)

    def test_release_is_audited_and_has_explicit_repository_context(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("python -m pip_audit --local", text)
        self.assertIn("GH_REPO: ${{ github.repository }}", text)

    def test_build_and_ci_use_patched_setuptools(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("setuptools>=83,<84", pyproject["build-system"]["requires"])

        for path in [
            ROOT / "action.yml",
            ROOT / ".github/workflows/dependency-scan.yml",
            ROOT / ".github/workflows/release.yml",
            ROOT / ".github/workflows/test.yml",
        ]:
            self.assertIn("setuptools==83.0.0", path.read_text(encoding="utf-8"), msg=str(path))


if __name__ == "__main__":
    unittest.main()
