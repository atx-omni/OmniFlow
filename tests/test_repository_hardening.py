import re
import tomllib
import unittest
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
PINNED_USE_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
SHA256_HASHES_RE = re.compile(r"--hash=sha256:[a-fA-F0-9]{64}(?:\s+--hash=sha256:[a-fA-F0-9]{64})*")


def hash_locked_requirements(text: str, *, allowed_includes: tuple[str, ...] = ()) -> list[Requirement]:
    """Validate the repository's lock format, associating hashes with each logical requirement."""
    logical_lines = []
    pending = ""
    for physical_line in text.splitlines():
        # A backslash in a full-line comment must not hide the next requirement.
        line = pending + ("" if physical_line.lstrip().startswith("#") else physical_line)
        if not physical_line.lstrip().startswith("#") and physical_line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        logical_lines.append(line)
        pending = ""
    if pending:
        raise ValueError("Lock file ends with an unfinished continuation")
    requirements = []
    for logical_line in logical_lines:
        line = re.split(r"\s+#", logical_line, maxsplit=1)[0].strip()
        if not line or line.startswith("#") or line in allowed_includes:
            continue
        requirement_text, separator, hashes = line.partition("--hash=")
        requirement = Requirement(requirement_text.strip())
        pins = list(requirement.specifier)
        if requirement.url or requirement.marker or len(pins) != 1 or pins[0].operator != "==":
            raise ValueError(f"Requirement must have one exact version pin: {line}")
        if "*" in pins[0].version:
            raise ValueError(f"Wildcard version is not an exact pin: {line}")
        if not SHA256_HASHES_RE.fullmatch(separator + hashes):
            raise ValueError(f"Requirement must have valid SHA-256 hashes: {line}")
        requirements.append(requirement)
    if not requirements:
        raise ValueError("Lock file has no pinned requirements")
    return requirements


class HashLockValidationTests(unittest.TestCase):
    def test_accepts_multiple_hashes_per_requirement_and_inline_hashes(self):
        text = (
            "# Release build inputs\n-r action-py311-linux-x86_64.txt\n\n"
            "build==1.6.0 \\\n"
            f"    --hash=sha256:{'a' * 64} \\\n"
            f"    --hash=sha256:{'b' * 64}\n"
            f"pyproject-hooks==1.2.0 --hash=sha256:{'c' * 64} # pinned build hook\n"
        )
        requirements = hash_locked_requirements(text, allowed_includes=("-r action-py311-linux-x86_64.txt",))
        self.assertEqual([str(requirement) for requirement in requirements], ["build==1.6.0", "pyproject-hooks==1.2.0"])

    def test_rejects_missing_hash_even_when_aggregate_counts_match(self):
        text = (
            "build==1.6.0 \\\n"
            f"    --hash=sha256:{'a' * 64} \\\n"
            f"    --hash=sha256:{'b' * 64}\n"
            "pyproject-hooks==1.2.0\n"
        )
        with self.assertRaisesRegex(ValueError, "valid SHA-256 hashes: pyproject-hooks"):
            hash_locked_requirements(text)

    def test_rejects_invalid_locks(self):
        valid_hash = f"--hash=sha256:{'a' * 64}"
        invalid_locks = (
            "# Empty lock\n",
            f"build>=1.6.0 {valid_hash}\n",
            f"build==1.* {valid_hash}\n",
            f"build==1.6.0; python_version<'3.12' {valid_hash}\n",
            f"build @ https://example.com/build.whl {valid_hash}\n",
            f"build==1.6.0 # {valid_hash}\n",
            f"build==1.6.0\n    {valid_hash}\n",
            f"{valid_hash}\nbuild==1.6.0 {valid_hash}\n",
            "build==1.6.0 --hash=sha256:abc\n",
            f"build==1.6.0 --hash=sha256:{'g' * 64}\n",
            f"build==1.6.0 --hash=sha1:{'a' * 40}\n",
            f"build==1.6.0 {valid_hash} --hash=sha256:abc\n",
            f"build==1.6.0 {valid_hash} \\",
            f"-r unchecked.txt\nbuild==1.6.0 {valid_hash}\n",
            f"# Comment with a backslash \\\nmissing==1.0\nbuild==1.6.0 {valid_hash}\n",
        )
        for text in invalid_locks:
            with self.subTest(text=text), self.assertRaises(ValueError):
                hash_locked_requirements(text)


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
    def test_distribution_and_cli_names_are_explicit(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["name"], "omniflow-ci")
        self.assertEqual(pyproject["project"]["scripts"]["omniflow"], "omniflow.cli:main")

    def test_all_first_party_workflow_actions_are_pinned_by_sha(self):
        workflow_paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
        workflow_paths.extend(sorted((ROOT / ".github/workflow-examples").glob("*.yml")))
        for path in workflow_paths:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            for use in nested_uses(payload):
                if use == "atx-omni/OmniFlow@<pinned-commit-sha>":
                    continue
                if use.startswith("./"):
                    continue
                self.assertRegex(use, PINNED_USE_RE, msg=f"Unpinned action in {path}: {use}")

    def test_composite_action_does_not_interpolate_inputs_inside_shell_scripts(self):
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
        scripts = [step.get("run", "") for step in action["runs"]["steps"]]
        self.assertFalse(any("${{ inputs." in script for script in scripts))
        self.assertTrue(any("--skip-reason" in script for script in scripts))
        self.assertTrue(any("omniflow route --auto" in script for script in scripts))
        self.assertTrue(any("omniflow repair ai --auto" in script for script in scripts))
        self.assertTrue(any("omniflow dbt sync --auto" in script for script in scripts))

    def test_example_workflow_uses_minimal_checkout_and_uploads_only_public_evidence(self):
        text = (ROOT / ".github/workflow-examples/omniflow.yml").read_text(encoding="utf-8")
        self.assertIn("fetch-depth: 1", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("timeout-minutes: 30", text)
        self.assertIn("cancel-in-progress: true", text)
        self.assertIn("pull_request_target:", text)
        self.assertIn("actions: read", text)
        self.assertIn("security-events: write", text)
        self.assertNotIn("ref: ${{ github.event.pull_request.head", text)
        self.assertIn("Route fork pull request without Omni secret", text)
        self.assertNotIn("skip-reason: Fork pull requests", text)
        self.assertIn("omni-api-key: ${{ secrets.OMNI_API_KEY }}", text)
        self.assertNotIn("env:\n          OMNI_API_KEY:", text)
        self.assertIn('.user.login == "github-actions[bot]"', text)
        self.assertIn(".omniflow/public/report.json", text)
        self.assertIn("vars.OMNIFLOW_UPLOAD_SARIF == 'true'", text)
        self.assertIn("continue-on-error: true", text)
        self.assertNotIn(".omniflow/restricted/", text)

    def test_every_checkout_disables_persisted_credentials(self):
        paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
        paths.extend(sorted((ROOT / ".github/workflow-examples").glob("*.yml")))
        for path in paths:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in payload.get("jobs", {}).values():
                for step in job.get("steps", []):
                    if str(step.get("uses", "")).startswith("actions/checkout@"):
                        self.assertFalse(step.get("with", {}).get("persist-credentials"), msg=str(path))

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
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("--require-hashes --only-binary=:all:", text)
        self.assertIn("requirements/release-py311-linux-x86_64.txt", text)
        self.assertIn("compare/${GITHUB_SHA}...main", text)
        self.assertIn("name: Signed release preflight", text)
        self.assertIn("github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", text)
        self.assertIn("name: omniflow-signed-release-preflight-${{ github.sha }}", text)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", text)
        self.assertIn("name: github-release", text)
        self.assertIn("GH_REPO: ${{ github.repository }}", text)
        self.assertNotIn("publish-pypi", text)
        self.assertNotIn("gh-action-pypi-publish", text)

    def test_build_and_ci_use_patched_setuptools(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        requirements = [Requirement(value) for value in pyproject["build-system"]["requires"]]
        setuptools = next(requirement for requirement in requirements if requirement.name == "setuptools")
        self.assertIsNone(setuptools.url)
        self.assertIsNone(setuptools.marker)
        self.assertTrue(
            any(spec.operator == ">=" and Version(spec.version) >= Version("83") for spec in setuptools.specifier)
        )
        self.assertTrue(any(spec.operator == "<" for spec in setuptools.specifier))
        lock = (ROOT / "requirements/action-py311-linux-x86_64.txt").read_text(encoding="utf-8")
        for source, text in [
            ("action lock", lock),
            *[
                (str(path), path.read_text(encoding="utf-8"))
                for path in (ROOT / ".github/workflows/dependency-scan.yml", ROOT / ".github/workflows/test.yml")
            ],
        ]:
            pins = re.findall(r"\bsetuptools==([0-9][A-Za-z0-9.!+_-]*)", text)
            self.assertTrue(pins, msg=source)
            for pin in pins:
                self.assertGreaterEqual(Version(pin), Version("83"), msg=source)
                self.assertIn(Version(pin), setuptools.specifier, msg=source)

    def test_ci_bootstrap_uses_patched_pip(self):
        for relative in ("dependency-scan.yml", "test.yml"):
            text = (ROOT / ".github/workflows" / relative).read_text(encoding="utf-8")
            pins = re.findall(r"\bpip==([0-9][A-Za-z0-9.!+_-]*)", text)
            self.assertTrue(pins, msg=relative)
            for pin in pins:
                self.assertGreaterEqual(Version(pin), Version("26.2"), msg=relative)

    def test_runtime_urllib3_floor_and_lock_are_patched(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        requirements = [Requirement(value) for value in pyproject["project"]["dependencies"]]
        urllib3 = next(requirement for requirement in requirements if requirement.name == "urllib3")
        self.assertIsNone(urllib3.url)
        self.assertIsNone(urllib3.marker)
        self.assertTrue(
            any(spec.operator == ">=" and Version(spec.version) >= Version("2.8.0") for spec in urllib3.specifier)
        )
        lock = (ROOT / "requirements/action-py311-linux-x86_64.txt").read_text(encoding="utf-8")
        locked = next(requirement for requirement in hash_locked_requirements(lock) if requirement.name == "urllib3")
        version = Version(next(iter(locked.specifier)).version)
        self.assertGreaterEqual(version, Version("2.8.0"))
        self.assertIn(version, urllib3.specifier)

    def test_action_install_is_hash_locked_and_token_is_runtime_only(self):
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
        self.assertNotIn("version", action["inputs"])
        self.assertIn("omni-api-key", action["inputs"])
        self.assertIn("repair-api-key", action["inputs"])
        self.assertIn("sync-api-key", action["inputs"])
        self.assertEqual(action["inputs"]["mode"]["default"], "validate")
        install = next(step for step in action["runs"]["steps"] if step["name"] == "Install OmniFlow")
        route = next(step for step in action["runs"]["steps"] if step["name"] == "Route OmniFlow run")
        run = next(step for step in action["runs"]["steps"] if step["name"] == "Run OmniFlow")
        repair = next(step for step in action["runs"]["steps"] if step["name"] == "Run OmniFlow AI repair")
        sync = next(step for step in action["runs"]["steps"] if step["name"] == "Synchronize dbt metadata into Omni")
        self.assertIn("--require-hashes", install["run"])
        self.assertIn("--only-binary=:all:", install["run"])
        self.assertIn("--no-deps --no-build-isolation", install["run"])
        self.assertIn("PIP_NO_INDEX=1", install["run"])
        self.assertNotIn("OMNI_API_KEY", install.get("env", {}))
        self.assertNotIn("OMNI_API_KEY", route.get("env", {}))
        self.assertIn("steps.route.outputs.should_run == 'true'", run["if"])
        self.assertEqual(run["env"]["OMNI_API_KEY"], "${{ inputs['omni-api-key'] }}")
        self.assertNotIn("OMNI_API_KEY", repair["env"])
        self.assertEqual(
            repair["env"]["OMNIFLOW_REPAIR_API_KEY"],
            "${{ inputs['repair-api-key'] }}",
        )
        self.assertNotIn("OMNIFLOW_REPAIR_API_KEY", install.get("env", {}))
        self.assertNotIn("OMNI_API_KEY", sync["env"])
        self.assertNotIn("OMNIFLOW_REPAIR_API_KEY", sync["env"])
        self.assertEqual(
            sync["env"]["OMNIFLOW_SYNC_API_KEY"],
            "${{ inputs['sync-api-key'] }}",
        )
        self.assertNotIn("OMNIFLOW_SYNC_API_KEY", install.get("env", {}))
        self.assertNotIn("omniflow-ci==", (ROOT / "action.yml").read_text(encoding="utf-8"))

    def test_ai_repair_workflow_is_manual_protected_and_same_repository_only(self):
        text = (ROOT / ".github/workflow-examples/omniflow-ai-repair.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", text)
        self.assertIn("types: [labeled]", text)
        self.assertIn("github.event.label.name == 'omniflow-ai-repair'", text)
        self.assertIn("head.repo.full_name == github.repository", text)
        self.assertIn("environment: omniflow-ai-repair", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("persist-credentials: false", text)
        self.assertNotIn("ref: ${{ github.event.pull_request.head", text)
        self.assertIn("mode: repair", text)
        self.assertIn("repair-api-key: ${{ secrets.OMNIFLOW_REPAIR_API_KEY }}", text)
        self.assertNotIn("OMNI_API_KEY", text)
        self.assertNotIn(".omniflow/restricted/", text)

    def test_dbt_sync_workflow_is_post_deployment_protected_and_loop_bounded(self):
        text = (ROOT / ".github/workflow-examples/omniflow-dbt-sync.yml").read_text(encoding="utf-8")
        self.assertIn("branches: [main]", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("environment: omniflow-production", text)
        self.assertIn("Deploy dbt to production", text)
        self.assertLess(text.index("Deploy dbt to production"), text.index("Synchronize dbt metadata into Omni"))
        self.assertIn("mode: dbt-sync", text)
        self.assertIn("sync-api-key: ${{ secrets.OMNIFLOW_SYNC_API_KEY }}", text)
        self.assertIn("models/**", text)
        self.assertNotIn("omni/**", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn(".omniflow/public/dbt-sync.json", text)
        self.assertNotIn(".omniflow/restricted/", text)

    def test_action_and_release_locks_pin_every_requirement_with_sha256(self):
        for relative, includes in (
            ("requirements/action-py311-linux-x86_64.txt", ()),
            ("requirements/release-py311-linux-x86_64.txt", ("-r action-py311-linux-x86_64.txt",)),
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(lock=relative):
                hash_locked_requirements(text, allowed_includes=includes)

    def test_security_critical_files_have_codeowners(self):
        text = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
        for path in ("/.github/", "/action.yml", "/requirements/", "/pyproject.toml", "/SECURITY.md"):
            self.assertIn(path, text)

    def test_actions_security_scanner_is_enabled(self):
        text = (ROOT / ".github/workflows/actions-security.yml").read_text(encoding="utf-8")
        self.assertIn("zizmorcore/zizmor-action@", text)
        self.assertIn("version: 1.29.0", text)
        self.assertIn("advanced-security: false", text)

    def test_simulator_uses_deterministic_base_branch(self):
        text = (ROOT / "scripts/simulate_alpha.py").read_text(encoding="utf-8")
        self.assertIn('["git", "init", "-q", "--initial-branch=main"]', text)


if __name__ == "__main__":
    unittest.main()
