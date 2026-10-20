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
def test_ancillary_git_yaml_is_ignored_by_default_and_rules_remain_supported(snapshot, side):
    default = verify_candidate(snapshot.client, snapshot.context, revision_side=side)
    assert default["status"] == "verified"
    assert default["file_count"] == 2
    proof = verify_candidate(snapshot.client, snapshot.context, revision_side=side, non_omni_rules=rules())
    assert proof["snapshot_digest"] == default["snapshot_digest"]
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


def test_nonmatching_rule_still_uses_default_source_filter(snapshot):
    proof = verify_candidate(snapshot.client, snapshot.context, non_omni_rules=rules("other/*.yaml"))
    assert proof["status"] == "verified"
    assert proof["file_count"] == 2
    assert proof["excluded_file_count"] == 0


def test_default_filter_does_not_drop_typed_sources_missing_from_api(snapshot):
    Path("omni/resources/missing.view.yaml").write_text("dimensions: {}\n")
    update_revision(snapshot)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == "file_inventory_mismatch"
    assert caught.value.candidate_counts["git_only_count"] == 1


@pytest.mark.parametrize("side", ["head", "base"])
@pytest.mark.parametrize("change", ["match", "changed", "missing"])
def test_api_authored_generic_yaml_is_still_verified(snapshot, side, change):
    snapshot.files["resources/config.yaml"] = (
        "synthetic: tooling\n" if change != "changed" else "synthetic: changed\n"
    )
    if change == "missing":
        snapshot.files["resources/missing.yaml"] = "synthetic: source\n"
    if change == "match":
        proof = verify_candidate(snapshot.client, snapshot.context, revision_side=side)
        assert proof["file_count"] == 3
    else:
        with pytest.raises(CandidateVerificationError) as caught:
            verify_candidate(snapshot.client, snapshot.context, revision_side=side)
        assert caught.value.candidate_reason == (
            "content_mismatch" if change == "changed" else "file_inventory_mismatch"
        )


@pytest.mark.parametrize("remote", [False, True])
def test_unrelated_files_are_not_read_or_counted_toward_source_limits(snapshot, monkeypatch, remote):
    from omniflow import candidate

    sha = snapshot.event["pull_request"]["head"]["sha"]
    entries = candidate._local_tree(sha, root=Path("."))
    entries.extend([
        {"path": f"omni/tooling/config-{i}.yaml", "type": "blob", "mode": "100644",
         "size": candidate.MAX_YAML_FILE_BYTES + 1, "sha": "a" * 40}
        for i in range(3)
    ])
    for name in ("README.md", "script.py", "query.sql", "data.csv", "data.json", ".DS_Store"):
        entries.append({"path": f"omni/{name}", "type": "blob", "mode": "100644",
                        "size": candidate.MAX_YAML_FILE_BYTES + 1, "sha": "b" * 40})
    monkeypatch.setattr(candidate, "MAX_YAML_FILES", 2)
    if remote:
        monkeypatch.setattr(candidate, "_local_tree", Mock(side_effect=candidate._GitUnavailable))
        monkeypatch.setattr(candidate, "_remote_tree", Mock(return_value=entries))
        original_read = candidate.read_git_text
        reader = Mock(side_effect=lambda repo, sha, path, **kwargs: original_read(sha, path, root=Path("."), **kwargs))
        monkeypatch.setattr(candidate, "read_github_text", reader)
    else:
        monkeypatch.setattr(candidate, "_local_tree", Mock(return_value=entries))
        reader = Mock(wraps=candidate.read_git_text)
        monkeypatch.setattr(candidate, "read_git_text", reader)
    proof = verify_candidate(snapshot.client, snapshot.context)
    assert proof["file_count"] == 2
    assert reader.call_count == 2
    read_paths = [call.args[2 if remote else 1] for call in reader.call_args_list]
    assert set(read_paths) == {"omni/model.yaml", "omni/orders.view.yaml"}


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
