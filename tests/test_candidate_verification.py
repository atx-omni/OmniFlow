import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow import candidate
from omniflow.candidate import CandidateVerificationError, verify_candidate


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("OMNIFLOW_GITHUB_TOKEN", "synthetic-token")
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository.request", Mock(side_effect=AssertionError("No live API calls")))
    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Synthetic test")
    files = {
        "model": "connection: synthetic\n",
        "relationships": "[]\n",
        "SCHEMA/orders.view": "dimensions: {id: {type: number}}\n",
        "QUERY/orders.query.view": "sql: select 1\n",
        "sales.topic": "base_view: schema__orders\n",
        "sales.composite_topic": "base_view: schema__orders\n",
        "legacy.relationships": "[]\n",
        "nested/settings.yaml": "type: model\n",
        "nested/more.yml": "type: model\n",
    }
    for name, text in files.items():
        path = Path("omni") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    Path("omni/README.md").write_text("Not an authored semantic file.\n")
    Path("untrusted.py").write_text("raise RuntimeError('never execute PR head')\n")
    git("add", ".")
    git("commit", "-m", "Synthetic immutable candidate fixture")
    head = git("rev-parse", "HEAD")
    event = {"pull_request": {"head": {"sha": head, "repo": {"full_name": "owner/repo"}}}}
    Path("event.json").write_text(json.dumps(event))
    context = SimpleNamespace(model_id="model", model_path="omni", branch_id="candidate-id",
                              branch_name="release-candidate", base_branch="main", environment="production")
    client = SimpleNamespace(get_model_yaml=Mock(return_value={"files": dict(files)}))
    return SimpleNamespace(files=files, context=context, client=client, head=head, event=event)


@pytest.mark.parametrize("wrapper", [None, "content", "contents", "both"])
def test_exact_candidate_uses_immutable_event_head_not_checkout_or_branch_name(snapshot, wrapper):
    files = snapshot.files
    if wrapper:
        files = {path: ({"content": text, "contents": text} if wrapper == "both" else {wrapper: text})
                 for path, text in files.items()}
    snapshot.client.get_model_yaml.return_value = {"files": files}
    Path("omni/SCHEMA/orders.view").write_text("This mutable checkout is not the PR revision.\n")
    git("add", "omni/SCHEMA/orders.view")
    git("commit", "-m", "Synthetic unrelated checkout revision")
    checkout_head = git("rev-parse", "HEAD")
    assert checkout_head != snapshot.head
    proof = verify_candidate(snapshot.client, snapshot.context)
    assert proof == verify_candidate(snapshot.client, snapshot.context)
    assert proof["status"] == "verified"
    assert proof["method"] == "authored_yaml_snapshot"
    assert proof["head_sha"] == snapshot.head
    assert proof["file_count"] == len(snapshot.files)
    assert len(proof["snapshot_digest"]) == 64
    assert git("rev-parse", "HEAD") == checkout_head
    snapshot.client.get_model_yaml.assert_called_with(
        "model", branch_id="candidate-id", mode="combined", include_checksums=False, fully_resolved=False,
    )


def test_root_model_scope_includes_all_semantic_files_but_not_readme_or_code(snapshot):
    snapshot.context.model_path = "."
    Path("root.view").write_text("dimensions: {}\n")
    Path(".omniflow.yml").write_text("checks: {}\n")
    Path(".github/workflows").mkdir(parents=True)
    Path(".github/workflows/omniflow.yml").write_text("name: OmniFlow\n")
    git("add", "root.view", ".omniflow.yml", ".github")
    git("commit", "-m", "Synthetic root-scoped semantic file")
    snapshot.event["pull_request"]["head"]["sha"] = git("rev-parse", "HEAD")
    Path("event.json").write_text(json.dumps(snapshot.event))
    files = {f"omni/{path}": text for path, text in snapshot.files.items()}
    files["root.view"] = "dimensions: {}\n"
    snapshot.client.get_model_yaml.return_value = {"files": files}
    assert verify_candidate(snapshot.client, snapshot.context)["file_count"] == len(files)


@pytest.mark.parametrize("change,reason", [
    ("stale", "content_mismatch"), ("missing", "file_inventory_mismatch"),
    ("deleted_still_in_api", "file_inventory_mismatch"),
])
def test_stale_missing_and_deleted_candidate_files_fail_closed(snapshot, change, reason):
    files = snapshot.client.get_model_yaml.return_value["files"]
    if change == "stale":
        files["SCHEMA/orders.view"] += "\n"  # Even seemingly benign formatting is not guessed away.
    elif change == "missing":
        del files["relationships"]
    else:
        files["deleted.view"] = "dimensions: {}\n"
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == reason
    assert "SCHEMA" not in str(caught.value)


@pytest.mark.parametrize("field,value,reason", [
    ("branch_id", None, "missing_candidate_branch"), ("model_path", "../omni", "unsafe_snapshot_path"),
])
def test_missing_branch_or_unsafe_scope_is_not_verified(snapshot, field, value, reason):
    setattr(snapshot.context, field, value)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == reason
    snapshot.client.get_model_yaml.assert_not_called()


@pytest.mark.parametrize("event", ["not_pr", "short_sha", "missing_repository", "malformed_head"])
def test_verification_requires_complete_immutable_pr_event(snapshot, monkeypatch, event):
    if event == "not_pr":
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    elif event == "short_sha":
        snapshot.event["pull_request"]["head"]["sha"] = snapshot.head[:7]
    elif event == "missing_repository":
        del snapshot.event["pull_request"]["head"]["repo"]
    else:
        snapshot.event["pull_request"]["head"] = None
    Path("event.json").write_text(json.dumps(snapshot.event))
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason in {"missing_pr_revision", "invalid_pr_revision"}
    snapshot.client.get_model_yaml.assert_not_called()


@pytest.mark.parametrize("malformed", ["null", "list", "bad_wrapper", "unsafe_path", "nonsemantic_extra"])
def test_malformed_api_snapshot_is_never_filtered_into_success(snapshot, malformed):
    payload = snapshot.client.get_model_yaml.return_value
    if malformed == "null":
        snapshot.client.get_model_yaml.return_value = None
    elif malformed == "list":
        payload["files"] = []
    elif malformed == "bad_wrapper":
        payload["files"]["model"] = {"content": "PRIVATE-CONTENT", "contents": "DIFFERENT-PRIVATE-CONTENT"}
    elif malformed == "unsafe_path":
        payload["files"]["../outside.view"] = "PRIVATE-CONTENT"
    else:
        payload["files"]["README.md"] = "PRIVATE-CONTENT"
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason in {"invalid_api_snapshot", "unsafe_snapshot_path"}
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("limit", ["tree", "file", "aggregate"])
def test_snapshot_bounds_are_enforced_before_verification(snapshot, monkeypatch, limit):
    if limit == "tree":
        monkeypatch.setattr(candidate, "MAX_TREE_BYTES", 32)
    elif limit == "file":
        monkeypatch.setattr(candidate, "MAX_YAML_FILE_BYTES", 64)
        snapshot.client.get_model_yaml.return_value["files"]["model"] = "x" * 65
    else:
        monkeypatch.setattr(candidate, "MAX_YAML_TOTAL_BYTES", 64)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == "snapshot_limit_exceeded"


def test_symlink_anywhere_under_model_scope_is_not_followed(snapshot):
    Path("omni/linked.view").symlink_to("../untrusted.py")
    git("add", "omni/linked.view")
    git("commit", "-m", "Synthetic symlink fixture")
    snapshot.event["pull_request"]["head"]["sha"] = git("rev-parse", "HEAD")
    Path("event.json").write_text(json.dumps(snapshot.event))
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == "unsafe_git_entry"
    snapshot.client.get_model_yaml.assert_not_called()


@pytest.mark.parametrize("response", ["complete", "duplicate_path", "truncated", "wrong_blob"])
def test_remote_fallback_requires_complete_duplicate_free_inventory_and_immutable_blobs(snapshot, monkeypatch, response):
    entries = []
    for path, text in snapshot.files.items():
        raw = text.encode()
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        entries.append({"path": f"omni/{path}", "type": "blob", "mode": "100644", "size": len(raw), "sha": sha})
    payload = {"tree": entries, "truncated": False}
    if response == "duplicate_path":
        entries.append(copy.deepcopy(entries[0]))
    elif response == "truncated":
        payload["truncated"] = True
    elif response == "wrong_blob":
        entries[0]["sha"] = "0" * 40
    monkeypatch.setattr(candidate, "_local_tree", Mock(side_effect=candidate._GitUnavailable))
    request = Mock(return_value=payload)
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository.request", request)
    read = Mock(side_effect=lambda repo, sha, path, **_: snapshot.files[path.removeprefix("omni/")])
    monkeypatch.setattr(candidate, "read_github_text", read)
    if response == "complete":
        assert verify_candidate(snapshot.client, snapshot.context)["head_sha"] == snapshot.head
        assert len(read.call_args_list) == len(snapshot.files)
        assert all(call.args[1] == snapshot.head for call in read.call_args_list)
    else:
        with pytest.raises(CandidateVerificationError) as caught:
            verify_candidate(snapshot.client, snapshot.context)
        assert caught.value.candidate_reason == {
            "duplicate_path": "duplicate_git_path", "truncated": "incomplete_git_snapshot",
            "wrong_blob": "incomplete_git_snapshot",
        }[response]
    request.assert_called_once_with("GET", f"/git/trees/{snapshot.head}?recursive=1", max_bytes=candidate.MAX_TREE_BYTES)


def _commit_snapshot(snapshot):
    git("add", "omni")
    git("commit", "-m", "Synthetic native export")
    sha = git("rev-parse", "HEAD")
    snapshot.event["pull_request"].update(
        head={"sha": sha, "repo": {"full_name": "owner/repo"}},
        base={"sha": sha, "repo": {"full_name": "owner/repo"}},
    )
    Path("event.json").write_text(json.dumps(snapshot.event))
    return sha


@pytest.mark.parametrize("wrapper", [".yaml", ".yml"])
@pytest.mark.parametrize("side", ["head", "base"])
def test_native_export_wrapper_matches_api_without_changing_git_proof(snapshot, wrapper, side):
    from omniflow.model_paths import authored_file_identity

    original_paths = {}
    for path, text in snapshot.files.items():
        wrapped = path + wrapper
        if authored_file_identity(wrapped) == path:
            Path("omni", path).rename(Path("omni", wrapped))
            original_paths[wrapped] = text
        else:
            original_paths[path] = text
    sha = _commit_snapshot(snapshot)
    proof = verify_candidate(snapshot.client, snapshot.context, revision_side=side)
    digest = hashlib.sha256()
    for path, text in sorted(original_paths.items()):
        name, raw = path.encode(), text.encode()
        digest.update(len(name).to_bytes(8, "big") + name + len(raw).to_bytes(8, "big") + raw)
    assert proof[f"{side}_sha"] == sha
    assert proof["file_count"] == len(original_paths)
    assert proof["snapshot_digest"] == digest.hexdigest()
    assert snapshot.client.get_model_yaml.call_args.kwargs["branch_id"] == (
        "candidate-id" if side == "head" else None
    )


@pytest.mark.parametrize("change,reason,counts", [
    ("git_collision", "ambiguous_file_identity", (10, 9)),
    ("api_collision", "ambiguous_file_identity", (9, 10)),
    ("double_wrapper", "file_inventory_mismatch", (9, 9)),
    ("generic_yaml", "file_inventory_mismatch", (9, 9)),
    ("case_difference", "file_inventory_mismatch", (9, 9)),
    ("changed_bytes", "content_mismatch", (9, 9)),
])
def test_filename_correspondence_does_not_weaken_verification(snapshot, change, reason, counts):
    api = snapshot.client.get_model_yaml.return_value["files"]
    old = Path("omni/SCHEMA/orders.view")
    if change == "git_collision":
        old.with_name("orders.view.yaml").write_text(old.read_text())
    elif change == "api_collision":
        api["SCHEMA/orders.view.yaml"] = api["SCHEMA/orders.view"]
        old.rename(old.with_name("orders.view.yaml"))
    elif change == "generic_yaml":
        api["nested/settings"] = api.pop("nested/settings.yaml")
        # A valid but unknown YAML basename is not aliased to its unwrapped name.
        api["nested/settings.yml"] = api.pop("nested/settings")
        old.rename(old.with_name("orders.view.yaml"))
    else:
        new = {"double_wrapper": "orders.view.yaml.yaml", "case_difference": "Orders.view.yaml",
               "changed_bytes": "orders.view.yaml"}[change]
        old.rename(old.with_name(new))
        if change == "changed_bytes":
            api["SCHEMA/orders.view"] += "# comment remains a byte-level difference\n"
    _commit_snapshot(snapshot)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    error = caught.value
    assert error.candidate_reason == reason
    assert (error.candidate_counts["git_file_count"], error.candidate_counts["omni_file_count"]) == counts
    assert "SCHEMA" not in str(error)
    if change in {"double_wrapper", "generic_yaml", "case_difference"}:
        assert error.candidate_counts["git_only_count"] == error.candidate_counts["omni_only_count"] == 1
    if change == "changed_bytes":
        assert error.candidate_counts["git_only_count"] == error.candidate_counts["omni_only_count"] == 0


def test_native_export_remote_fallback_reads_original_git_paths(snapshot, monkeypatch):
    from omniflow.model_paths import authored_file_identity

    remote_files = {path + ".yaml" if authored_file_identity(path + ".yaml") == path else path: text
                    for path, text in snapshot.files.items()}
    entries = []
    for path, text in remote_files.items():
        raw = text.encode()
        entries.append({"path": "omni/" + path, "type": "blob", "mode": "100644", "size": len(raw),
                        "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()})
    monkeypatch.setattr(candidate, "_local_tree", Mock(side_effect=candidate._GitUnavailable))
    monkeypatch.setattr(candidate, "_remote_tree", Mock(return_value=entries))
    reader = Mock(side_effect=lambda repo, sha, path, **_: remote_files[path.removeprefix("omni/")])
    monkeypatch.setattr(candidate, "read_github_text", reader)
    assert verify_candidate(snapshot.client, snapshot.context)["file_count"] == 9
    assert {call.args[2] for call in reader.call_args_list} == {"omni/" + path for path in remote_files}
