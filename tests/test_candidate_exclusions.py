import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow.candidate import CandidateVerificationError, _mismatch_category_counts, verify_candidate
from omniflow.file_scope import parse_non_omni_rules


def git(*args):
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()


def rules(pattern="omni/resources/*.yaml"):
    return parse_non_omni_rules([{
        "tag": "ancillary-config", "pattern": pattern,
        "reason": "Synthetic tooling configuration, not Omni source",
    }])


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("requests.sessions.Session.request", Mock(side_effect=AssertionError("No live requests")))
    git("init", "-b", "main")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "synthetic@example.invalid")
    files = {"model": "connection: synthetic\n", "orders.view": "dimensions: {}\n"}
    for name, text in files.items():
        path = Path("omni", name + ".yaml")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    ancillary = Path("omni/resources/config.yaml")
    ancillary.parent.mkdir()
    ancillary.write_text("synthetic: tooling\n")
    git("add", "omni")
    git("commit", "-m", "Synthetic scope fixture")
    sha = git("rev-parse", "HEAD")
    event = {"pull_request": {
        side: {"sha": sha, "repo": {"full_name": "synthetic/repo"}} for side in ("base", "head")
    }}
    Path("event.json").write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    context = SimpleNamespace(model_id="model", model_path="omni", branch_id="candidate")
    client = SimpleNamespace(get_model_yaml=Mock(return_value={"files": files}))
    return SimpleNamespace(client=client, context=context, event=event, files=files)


def update_revision(snapshot):
    git("add", "omni")
    git("commit", "-m", "Synthetic fixture update")
    snapshot.event["pull_request"]["head"]["sha"] = git("rev-parse", "HEAD")
    Path("event.json").write_text(json.dumps(snapshot.event))


@pytest.mark.parametrize("side", ["head", "base"])
def test_only_trusted_ancillary_git_yaml_is_excluded(snapshot, side):
    with pytest.raises(CandidateVerificationError, match="file_inventory_mismatch"):
        verify_candidate(snapshot.client, snapshot.context, revision_side=side)
    proof = verify_candidate(snapshot.client, snapshot.context, revision_side=side, non_omni_rules=rules())
    assert proof["status"] == "verified"
    assert proof["file_count"] == 2
    assert proof["excluded_file_count"] == 1
    assert "resources" not in json.dumps(proof)
    assert "tooling" not in json.dumps(proof)


def test_excluding_ancillary_yaml_does_not_hide_changed_omni_bytes(snapshot):
    snapshot.files["orders.view"] += "# changed remotely\n"
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules())
    assert caught.value.candidate_reason == "content_mismatch"


def test_api_authored_generic_yaml_cannot_be_excluded(snapshot):
    snapshot.files["resources/config.yaml"] = "synthetic: tooling\n"
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules())
    assert caught.value.candidate_reason == "non_omni_source_conflict"
    assert caught.value.validation_scope == "branch"
    assert "resources" not in str(caught.value)


def test_opposite_side_api_source_is_checked_even_without_matching_git_file(snapshot):
    def api(*args, branch_id=None, **kwargs):
        files = dict(snapshot.files)
        if branch_id is None:
            files["resources/deleted.yaml"] = "synthetic: source\n"
        return {"files": files}

    snapshot.client.get_model_yaml.side_effect = api
    assert verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules())["status"] == "verified"
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context, revision_side="base", non_omni_rules=rules())
    assert caught.value.candidate_reason == "non_omni_source_conflict"
    assert caught.value.validation_scope == "base"


def test_glob_cannot_hide_git_typed_file_missing_from_api(snapshot):
    Path("omni/resources/deleted.view.yaml").write_text("dimensions: {}\n")
    update_revision(snapshot)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules())
    assert caught.value.candidate_reason == "non_omni_source_conflict"


def test_native_api_alias_is_protected_without_a_git_counterpart(snapshot):
    snapshot.files["resources/deleted.view"] = "dimensions: {}\n"
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules())
    assert caught.value.candidate_reason == "non_omni_source_conflict"


def test_exclusions_do_not_hide_unsafe_git_entries(snapshot):
    Path("omni/resources/config.yaml").unlink()
    Path("omni/resources/config.yaml").symlink_to("../../outside.yaml")
    update_revision(snapshot)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules())
    assert caught.value.candidate_reason == "unsafe_git_entry"


def test_nonmatching_rule_cannot_relax_full_inventory_comparison(snapshot):
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules("other/*.yaml"))
    assert caught.value.candidate_reason == "file_inventory_mismatch"
    assert caught.value.candidate_counts["git_only_count"] == 1


@pytest.mark.parametrize("git_paths,api_paths,expected", [
    ({"SCHEMA/orders.view"}, {"schema/orders.view"}, (1, 0, 0)),
    ({"old/orders.view"}, {"new/orders.view"}, (0, 1, 0)),
    ({"nested/model.yaml"}, {"nested/model"}, (0, 0, 1)),
    ({"settings.yaml"}, {"settings.yml"}, (0, 0, 1)),
    ({"orders.view.yaml.yaml"}, {"orders.view"}, (0, 0, 1)),
    ({"a/orders.view", "b/orders.view"}, {"c/orders.view"}, (0, 0, 0)),
    ({"nested/Model.yaml"}, {"nested/model"}, (0, 0, 0)),
    ({"old.view"}, {"new.view"}, (0, 0, 0)),
])
def test_mismatch_categories_only_describe_unique_pairs(git_paths, api_paths, expected):
    counts = _mismatch_category_counts(git_paths, api_paths)
    assert tuple(counts[name] for name in ("case_only_count", "directory_only_count", "suffix_only_count")) == expected
    # Descriptive normalization must not change the source sets used for proof.
    assert git_paths != api_paths


def test_safe_mismatch_categories_do_not_turn_case_difference_into_a_pass(snapshot):
    snapshot.files["Orders.view"] = snapshot.files.pop("orders.view")
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules())
    error = caught.value
    assert error.candidate_reason == "file_inventory_mismatch"
    assert error.candidate_counts["case_only_count"] == 1
    assert error.candidate_counts["git_only_count"] == error.candidate_counts["omni_only_count"] == 1
    assert "Orders.view" not in str(error)
