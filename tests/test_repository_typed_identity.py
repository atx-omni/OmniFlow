import json
import subprocess
from pathlib import Path

import pytest

from omniflow.artifacts import write_public_reports
from omniflow.discovery import ModelContext
from omniflow.repository_diagnostics import (
    _load_snapshot,
    finish_repository_context,
    repository_reporting,
    start_repository_context,
)
from omniflow.repository_policy import RepositoryPolicy


def view(schema):
    return f"schema: {schema}\ntable_name: USERS\ndimensions:\n  id:\n    sql: private_source_literal\n"


@pytest.fixture
def typed_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Synthetic test")
    git("config", "user.email", "synthetic@example.invalid")
    context = ModelContext(model_id="private-model", base_url="https://synthetic.omniapp.co",
                           model_path="omni", branch_id="candidate", branch_name="candidate")
    Path(".omni").mkdir()
    Path(".omni/flow.json").write_text(json.dumps({"version": 1, "models": [vars(context)]}))
    files = {
        "Git Export/sales/users.view.yaml": view("SALES"),
        "Git Export/marketing/users.view.yml": view("MARKETING"),
        "Git Topics/overview.topic.yaml": "name: overview\nbase_view: sales__users\nlabel: private_topic_literal\n",
    }
    for path, text in files.items():
        target = Path("omni", path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git("add", ".")
    git("commit", "-m", "Synthetic typed base")
    base = git("rev-parse", "HEAD")
    head_files = {path: "# Candidate\n" + text for path, text in files.items()}
    for path, text in head_files.items():
        Path("omni", path).write_text(text)
    git("add", "omni")
    git("commit", "-m", "Synthetic typed head")
    head = git("rev-parse", "HEAD")
    policy = RepositoryPolicy(True, "repository_verified", "example/private", head, base)
    monkeypatch.setattr("omniflow.repository_diagnostics.resolve_repository_policy", lambda config: policy)

    class Client:
        invalid_metadata = False
        changed = False
        renamed = False

        def get_model_yaml(self, model_id, *, branch_id, **kwargs):
            source = head_files if branch_id else files
            payload = {"files": {
                "api/first/users.view": source["Git Export/sales/users.view.yaml"],
                "api/second/users.view": source["Git Export/marketing/users.view.yml"],
                "api/topics/overview.topic": source["Git Topics/overview.topic.yaml"],
            }, "viewNames": {"sales__users": "api/first/users.view", "marketing__users": "api/second/users.view"}}
            if self.invalid_metadata:
                payload["viewNames"]["sales__users"] = "not-an-inventory-path.view"
            if self.changed:
                payload["files"]["api/first/users.view"] += "# Changed after evidence\n"
            if self.renamed:
                payload["viewNames"]["renamed__users"] = payload["viewNames"].pop("sales__users")
            return payload

    return context, Client(), policy


def proofs_for(context, policy, enabled):
    if not enabled:
        return {}
    proofs = {}
    for side in ("head", "base"):
        snapshot = _load_snapshot(policy, context.model_path, side)
        proofs[f"{side}_proof"] = {
            "status": "verified", "method": "authored_yaml_snapshot", f"{side}_sha": snapshot.sha,
            "snapshot_digest": snapshot.digest, "file_count": len(snapshot.files),
        }
    return proofs


@pytest.mark.parametrize("use_proofs", [False, True])
def test_moved_qualified_duplicate_views_and_topic_keep_private_original_locations(typed_repository, use_proofs):
    context, client, policy = typed_repository
    proofs = proofs_for(context, policy, use_proofs)
    issues = [
        {"validator": "model", "severity": "error", "yaml_path": "api/first/users.view,dimensions,id,sql"},
        {"validator": "content", "severity": "error", "field": "marketing__users.id"},
        {"validator": "content", "severity": "error", "field": "sales__users.id", "state": "resolved"},
        {"validator": "model", "severity": "error", "yaml_path": "api/topics/overview.topic,base_view"},
    ]
    report = {"exit_code": 1, "validation_complete": True, "issues": issues}
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        evidence = start_repository_context(context, client, branch_id="candidate", **proofs)
        assert evidence is not None and set(evidence.snapshots) == {"head", "base"}
        finish_repository_context(evidence, context, client, issues, branch_id="candidate", **proofs)
        safe = write_public_reports(report, output_dir=".omniflow", formats=["json", "markdown", "sarif", "junit"],
                                    redaction_level="standard")
    private = json.loads(Path(".omniflow/repository/report.json").read_text())
    assert [(issue["file"], issue["line"], issue["source_revision"]) for issue in private["issues"]] == [
        ("omni/Git Export/sales/users.view.yaml", 6, "head"),
        ("omni/Git Export/marketing/users.view.yml", 5, "head"),
        ("omni/Git Export/sales/users.view.yaml", 4, "base"),
        ("omni/Git Topics/overview.topic.yaml", 3, "head"),
    ]
    assert private["issues"][0]["field"] == "sales__users.id"
    assert safe["exit_code"] == private["exit_code"] == 1
    for path in Path(".omniflow").rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert "private_source_literal" not in text and "private_topic_literal" not in text
            if path.parent.name != "repository":
                assert "users.view" not in text and "sales__users" not in text and "Git Export" not in text


@pytest.mark.parametrize("use_proofs", [False, True])
@pytest.mark.parametrize("fault", ["invalid_metadata", "changed", "renamed"])
def test_fresh_alias_evidence_is_required_even_with_a_candidate_proof(typed_repository, use_proofs, fault):
    context, client, policy = typed_repository
    proofs = proofs_for(context, policy, use_proofs)
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        evidence = start_repository_context(context, client, branch_id="candidate", **proofs)
        assert evidence is not None
        setattr(client, fault, True)
        issues = [{"validator": "content", "severity": "error", "field": "sales__users.id"}]
        finish_repository_context(evidence, context, client, issues, branch_id="candidate", **proofs)
        safe = write_public_reports({"exit_code": 1, "issues": issues}, output_dir=".omniflow",
                                    formats=["json"], redaction_level="standard")
    assert not Path(".omniflow/repository/report.json").exists()
    assert safe["repository_diagnostics"]["status"] == "unavailable"


def test_candidate_identity_digest_must_match_fresh_reporting_correspondence(typed_repository):
    context, client, policy = typed_repository
    proofs = proofs_for(context, policy, True)
    for proof in proofs.values():
        proof["identity_digest"] = "0" * 64
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        assert start_repository_context(context, client, branch_id="candidate", **proofs) is None
