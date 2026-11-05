import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow import candidate_diagnosis
from omniflow.candidate_difference import difference_details
from omniflow.cli import main


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def local(tmp_path, monkeypatch, request):
    monkeypatch.chdir(tmp_path)
    for key in ("CI", "GITHUB_ACTIONS", "GITHUB_EVENT_NAME", "GITHUB_API_URL", "GITLAB_CI", "BUILDKITE", "TF_BUILD",
                "JENKINS_URL", "OMNI_MODEL_ID", "OMNI_BRANCH_NAME", "OMNI_BRANCH_ID"):
        monkeypatch.delenv(key, raising=False)
    tenant = "https://fixture.omniapp.co"
    secret = "fixture-private-token"
    monkeypatch.setenv("OMNI_BASE_URL", tenant)
    monkeypatch.setenv("OMNI_API_KEY", secret)
    monkeypatch.setenv("OMNIFLOW_GITHUB_TOKEN", "fixture-read-github")
    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Local diagnostic test")
    git("config", "core.autocrlf", "false")
    Path(".omni").mkdir()
    model = {"model_id": "synthetic-model", "model_path": "omni/sales", "base_url": tenant, "base_branch": "main",
             "git_provider": "github", "web_url": "https://github.com/example/fixture"}
    flow = {"version": 1, "models": [model]}
    if getattr(request, "param", None) == "v2":
        flow["version"] = 2
        model.update(environment="prod", git_follower=True)
    Path(".omni/flow.json").write_text(json.dumps(flow))
    Path(".omniflow.yml").write_text("validation: {mode: strict}\n")
    Path("policies").mkdir()
    Path("policies/checks.yml").write_text("validation: {mode: strict}\n")
    files = {"aaa.topic": "base_view: orders\n", "orders.view": "label: private-raw-value\n",
             "zzz.topic": "base_view: other\n"}
    for path, text in files.items():
        target = Path("omni/sales") / (path + ".yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git("add", ".")
    git("commit", "-m", "Trusted base")
    base = git("rev-parse", "HEAD")
    git("switch", "-c", "synthetic/pr")
    Path("omni/sales/orders.view.yaml").write_text(files["orders.view"] + "# private-comment\n")
    flow["models"][0]["base_url"] = "https://untrusted.example"
    Path(".omni/flow.json").write_text(json.dumps(flow))
    Path("policies/checks.yml").write_text("not_a_supported_policy_key: true\n")
    git("add", ".")
    git("commit", "-m", "PR head data, never executed")
    head = git("rev-parse", "HEAD")
    git("switch", "main")
    # A dirty worktree must not redirect the request, either.
    Path(".omni/flow.json").write_text(json.dumps(flow))
    Path("policies/checks.yml").write_text("not_a_supported_policy_key: true\n")
    repo = {"id": 9, "full_name": "example/fixture", "default_branch": "main"}
    pr = {"number": 7, "state": "open", "base": {"sha": base, "ref": "main", "repo": repo},
          "head": {"sha": head, "ref": "synthetic/pr", "repo": repo}}
    def github(method, path, **kwargs):
        assert method == "GET"
        assert path in {"", "/pulls/7"}
        return repo if not path else pr
    gh = Mock()
    gh.request.side_effect = github
    monkeypatch.setattr(candidate_diagnosis, "GitHubRepository", Mock(return_value=gh))
    client = Mock()
    client.get_model_yaml.return_value = {"files": files}
    client.resolve_branch_id.return_value = "candidate-id"
    client.get_git_configuration.return_value = {
        "baseBranch": "main", "gitServiceProvider": "github", "gitFollower": True, "branchPerPullRequest": True,
        "webUrl": "https://github.com/example/fixture", "modelPath": "/omni/sales",
    }
    client.list_models.return_value = [{
        "modelKind": "BRANCH", "baseModelId": "synthetic-model", "name": "synthetic/pr", "id": "candidate-id",
    }]
    factory = Mock(return_value=client)
    monkeypatch.setattr(candidate_diagnosis, "OmniClient", factory)
    monkeypatch.setattr("omniflow.cli.OmniClient", factory)
    argv = ["candidate", "diagnose", "--repository", "example/fixture", "--pr", "7", "--head-sha", head]
    return SimpleNamespace(argv=argv, base=base, head=head, client=client, factory=factory,
                           secret=secret, tenant=tenant, gh=gh, pr=pr)


def test_local_comparison_uses_registered_base_and_immutable_head_and_keeps_data_local(local, capsys):
    assert main(local.argv) == 2
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert (data["status"], data["git_revision"], data["scope"]) == ("different", local.head, "branch")
    assert (data["compared_file_count"], data["files_not_compared"], data["candidate_sample_count"]) == (2, 1, 1)
    assert data["samples"] == [{"git_path": "omni/sales/orders.view.yaml", "omni_path": "orders.view",
                                "pairing": "authored_identity", "comparison": "different", "first_differing_line": 2,
                                "difference_class": "yaml_comment_or_whitespace_only"}]
    assert data["not_a_validation_run"]
    local.factory.assert_called_once_with(base_url=local.tenant, timeout=60, api_key=local.secret)
    local.client.get_model_yaml.assert_called_once_with("synthetic-model", branch_id="candidate-id",
                                                      mode="combined", include_checksums=False, fully_resolved=False)
    assert all(value not in captured.out + captured.err for value in
               (local.secret, "fixture-read-github", "private-raw-value", "private-comment", "untrusted.example"))
    assert not Path(".omniflow").exists()
    assert git("rev-parse", "HEAD") == local.base


def test_base_scope_requires_exact_base_and_reads_shared_yaml_without_branch_resolution(local, capsys):
    assert main([*local.argv, "--scope", "base"]) == 2
    local.gh.request.assert_not_called()
    assert main([*local.argv, "--scope", "base", "--base-sha", local.base]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "matched" and result["git_revision"] == local.base
    assert result["compared_file_count"] == 3 and result["not_a_validation_run"]
    local.client.resolve_branch_id.assert_not_called()
    assert local.client.get_model_yaml.call_args.kwargs["branch_id"] is None


@pytest.mark.parametrize("guard", ["ci", "origin", "tenant", "fork", "stale", "missing-head", "drift", "nondefault"])
def test_unsafe_or_unverifiable_diagnoses_do_not_emit_samples(local, capsys, monkeypatch, guard):
    if guard == "ci":
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    elif guard == "origin":
        monkeypatch.setenv("GITHUB_API_URL", "https://other.example")
    elif guard == "tenant":
        monkeypatch.setenv("OMNI_BASE_URL", "https://different.omniapp.co")
    elif guard == "fork":
        local.pr["head"]["repo"] = {"id": 19, "full_name": "fork/repo"}
    elif guard == "nondefault":
        local.pr["base"]["ref"] = "staging"
    elif guard == "missing-head":
        local.pr["head"]["sha"] = local.argv[-1] = "1" * 40
    elif guard == "drift":
        original = local.gh.request.side_effect
        count = 0
        def move(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                local.pr["head"]["sha"] = "2" * 40
            return original(*args, **kwargs)
        local.gh.request.side_effect = move
    else:
        local.argv[-1] = "3" * 40
    assert main(local.argv) != 0
    stdout, stderr = capsys.readouterr()
    assert "orders.view" not in stdout + stderr and local.secret not in stdout + stderr
    if guard == "missing-head":
        assert "Fetch" in json.loads(stdout)["next_action"]
    else:
        assert not stdout
    if guard != "drift" and guard != "missing-head":
        local.factory.assert_not_called()
    if guard in {"ci", "origin"}:
        local.gh.request.assert_not_called()


@pytest.mark.parametrize("git_text,omni_text,kind,line", [
    ("a: 1\r\nb: 2\r\n", "a: 1\nb: 2\n", "line_endings_only", 1),
    ("a: 1\n", "a: 1", "terminal_newline_only", 1),
    ("a: 1\n# note\n", "a: 1\n", "yaml_comment_or_whitespace_only", 2),
    ("a: 'x#1'\n", "a: 'x#2'\n", "authored_text_changed", 1),
    ("sql: |\n  SELECT 1\n", "sql: |\n   SELECT 2\n", "authored_text_changed", 2),
    ("a: [\n", "a: 1\n", "unclassified_text_difference", 1),
])
def test_local_difference_categories_do_not_waive_exact_equality(git_text, omni_text, kind, line):
    result = difference_details(git_text, omni_text)
    assert (result["first_differing_line"], result["difference_class"]) == (line, kind)


def test_sensitive_or_unsafe_paths_do_not_print_raw_metadata(local):
    assert candidate_diagnosis._path_label(f"omni/{local.secret}.view") == "[withheld]"
    assert candidate_diagnosis._path_label("foo/\x1b[31m.view") == "[withheld]"


@pytest.mark.parametrize("local", ["v2"], indirect=True)
@pytest.mark.parametrize("mode", ["branch", "base", "wrong-environment", "wrong-live-repo", "duplicate-branch"])
def test_v2_uses_governed_target_and_immutable_custom_policy(local, monkeypatch, capsys, mode):
    monkeypatch.setenv("OMNIFLOW_ENVIRONMENT", "staging" if mode == "wrong-environment" else "prod")
    if mode == "wrong-live-repo":
        local.client.get_git_configuration.return_value["webUrl"] = "https://github.com/example/wrong"
    if mode == "duplicate-branch":
        local.client.list_models.return_value *= 2
    argv = [*local.argv, "--model-path", "omni/sales", "--config", "policies/checks.yml"]
    if mode == "base":
        argv += ["--scope", "base", "--base-sha", local.base]
    result = main(argv)
    if mode in {"branch", "base"}:
        assert result == (2 if mode == "branch" else 0)
        assert json.loads(capsys.readouterr().out)["git_revision"] == (local.head if mode == "branch" else local.base)
        local.client.get_git_configuration.assert_called_once_with("synthetic-model")
        expected_branch = "candidate-id" if mode == "branch" else None
        assert local.client.get_model_yaml.call_args.kwargs["branch_id"] == expected_branch
        if mode == "branch":
            local.client.list_models.assert_called_once_with(model_kind="BRANCH", base_model_id="synthetic-model",
                                                            name="synthetic/pr")
        else:
            local.client.list_models.assert_not_called()
    else:
        assert result != 0
        assert not capsys.readouterr().out
        local.client.get_model_yaml.assert_not_called()
        if mode == "wrong-environment":
            local.factory.assert_not_called()


@pytest.mark.parametrize("path", ["../private.yml", "/absolute.yml", "./policies/checks.yml", "missing.yml"])
def test_custom_policy_requires_exact_regular_base_blob(local, capsys, path):
    assert main([*local.argv, "--config", path]) != 0
    assert not capsys.readouterr().out
    local.factory.assert_not_called()


def test_local_git_invocations_only_read_objects_without_protocols_or_prompts(local, capsys, monkeypatch):
    popen = Mock(wraps=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", popen)
    assert main(local.argv) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "content_mismatch"
    assert popen.call_count > 0
    for call in popen.call_args_list:
        argv, env = call.args[0], call.kwargs["env"]
        assert "--no-replace-objects" in argv and "--no-optional-locks" in argv
        assert "protocol.allow=never" in argv and "gc.auto=0" in argv
        assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert set(argv).intersection({"ls-tree", "rev-parse", "cat-file"})


def test_show_diff_refuses_captured_output_and_ci_without_remote_calls(local, capsys, monkeypatch):
    monkeypatch.setattr(candidate_diagnosis, "_interactive_terminal", lambda: False)
    assert main([*local.argv, "--show-diff"]) == 2
    assert "interactive terminal" in capsys.readouterr().err
    monkeypatch.setattr(candidate_diagnosis, "_interactive_terminal", lambda: True)
    monkeypatch.setenv("CI", "true")
    assert main([*local.argv, "--show-diff"]) == 2
    local.gh.request.assert_not_called()


def test_explicit_terminal_diff_reveals_only_first_differing_line_after_bound_comparison(local, capsys, monkeypatch):
    monkeypatch.setattr(candidate_diagnosis, "_interactive_terminal", lambda: True)
    files = local.client.get_model_yaml.return_value["files"]
    files["orders.view"] = "label: intended text\nother: hidden-second-line\n"
    assert main([*local.argv, "--show-diff"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["source_preview"] == {"git": "label: private-raw-value", "omni": "label: intended text"}
    assert "hidden-second-line" not in captured.out + captured.err
    assert main(local.argv) == 2
    assert "private-raw-value" not in capsys.readouterr().out
    assert not Path(".omniflow").exists()


@pytest.mark.parametrize("sensitive", ["fixture-private-token", "password: imaginary", "ghp_secret-canary",
                                       "123-45-6789", "local-external-secret"])
def test_terminal_source_preview_screens_whole_line_before_truncation(local, sensitive, monkeypatch):
    monkeypatch.setenv("OTHER_ACCESS_KEY", "local-external-secret")
    preview = candidate_diagnosis._terminal_preview(
        "x: value\n", "x: value" + " " * 240 + sensitive + "\n",
        {"first_differing_line": 1, "difference_class": "authored_text_changed"},
    )
    assert preview["omni"] == "[withheld: sensitive pattern]"


def test_terminal_preview_skips_newline_only_and_bounds_long_lines():
    assert not candidate_diagnosis._terminal_preview("x", "x\n", {"difference_class": "terminal_newline_only"})
    preview = candidate_diagnosis._terminal_preview(
        "x " * 160 + "left", "x " * 160 + "right",
        {"first_differing_line": 1, "difference_class": "authored_text_changed"},
    )
    assert all(len(value) <= candidate_diagnosis.MAX_PREVIEW_CHARS for value in preview.values())
    assert "left" in preview["git"] and "right" in preview["omni"]
