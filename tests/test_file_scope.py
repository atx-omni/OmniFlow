import json
import subprocess
from pathlib import Path

import pytest

from omniflow.config import _to_config, load_config
from omniflow.exceptions import ConfigError
from omniflow.file_scope import (
    FileScopeError,
    classify_repo_file,
    excluded_semantic_paths,
    matching_non_omni_rule,
    parse_non_omni_rules,
)


def rule(pattern="omni/resources/**/*.yaml", tag="ancillary"):
    return {"tag": tag, "pattern": pattern, "reason": "Synthetic repository configuration, not Omni source"}


def test_default_config_is_backward_compatible():
    config = _to_config({}, None)
    assert config.files.non_omni == []
    assert config.validation.mode == "strict"
    assert config.content_validation.targeted is False


def test_new_policy_settings_load_without_environment_exclusion_overrides(tmp_path, monkeypatch):
    path = tmp_path / ".omniflow.yml"
    path.write_text(json.dumps({
        "files": {"non_omni": [rule()]},
        "validation": {"mode": "advisory"},
        "checks": {"content_validation": {"targeted": True}},
    }))
    monkeypatch.setenv("OMNIFLOW_NON_OMNI", "**")
    config = load_config(path)
    assert config.files.non_omni[0].pattern == "omni/resources/**/*.yaml"
    assert config.validation.mode == "advisory"
    assert config.content_validation.targeted is True


@pytest.mark.parametrize("payload", [
    {"validation": {"mode": "skip"}}, {"validation": {"mode": True}},
    {"validation": {"mode": None}}, {"validation": {"ignore": True}},
    {"files": {"exclude": ["omni/**"]}}, {"files": {"non_omni": "omni/resources/config.yaml"}},
    {"checks": {"content_validation": {"targeted": "maybe"}}},
])
def test_invalid_policy_controls_fail_closed(tmp_path, payload):
    path = tmp_path / ".omniflow.yml"
    path.write_text(json.dumps(payload))
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("pattern", [
    "", " ", "../settings.yaml", "/settings.yaml", "omni//settings.yaml", "omni/./settings.yaml",
    "omni/../settings.yaml", "omni\\settings.yaml", "C:/settings.yaml", "**", "**/*.yaml", "*.yaml",
    "omni/**", "omni/*", "omni/**/../*.yaml", "omni/**/**/config.yaml", "omni/a**b/config.yaml",
    "omni/[ab].yaml", "omni/?.yaml", "!omni/settings.yaml", "omni/settings.yaml\n", "${MODEL}/settings.yaml",
])
def test_unsafe_or_blanket_patterns_are_rejected(pattern):
    with pytest.raises(ConfigError):
        parse_non_omni_rules([rule(pattern)])


@pytest.mark.parametrize("entry", [
    {}, {"pattern": "omni/resources/config.yaml", "tag": "missing-reason"},
    {**rule(), "ignore": True}, {**rule(), "tag": "Contains spaces"},
    {**rule(), "reason": ""}, {**rule(), "reason": "line one\nline two"},
    {**rule(), "reason": "x" * 241}, {**rule(), "pattern": "x" * 1025},
])
def test_invalid_rule_records_are_rejected_without_repeating_values(entry):
    with pytest.raises(ConfigError):
        parse_non_omni_rules([entry])


def test_duplicate_rules_and_literal_overlap_are_rejected():
    for entries in (
        [rule(), rule(tag="another")],
        [rule(), rule("omni/config.yaml")],
        [rule(), rule("omni/resources/config.yaml", "config")],
        [rule(f"omni/config{i}.yaml", f"config{i}") for i in range(101)],
    ):
        with pytest.raises(ConfigError):
            parse_non_omni_rules(entries)


@pytest.mark.parametrize("path,expected", [
    ("omni/resources/config.yaml", True), ("omni/resources/sub/config.yaml", True),
    ("omni/resources/雪/config.yaml", True), ("omni/resources/A Space/config.yaml", True),
    ("omni/resources/config.yml", False), ("Omni/resources/config.yaml", False),
    ("other/omni/resources/config.yaml", False), ("omni/Resources/config.yaml", False),
])
def test_case_sensitive_repository_anchored_patterns(path, expected):
    rules = parse_non_omni_rules([rule()])
    assert bool(matching_non_omni_rule(path, rules)) is expected


def test_single_star_does_not_cross_directory_boundaries():
    rules = parse_non_omni_rules([rule("omni/resources/*.yaml")])
    assert matching_non_omni_rule("omni/resources/config.yaml", rules)
    assert matching_non_omni_rule("omni/resources/sub/config.yaml", rules) is None


def test_overlapping_globs_fail_without_arbitrary_precedence():
    rules = parse_non_omni_rules([
        rule("omni/resources/*.yaml"), rule("omni/resources/config*.yaml", "config"),
    ])
    with pytest.raises(FileScopeError, match="non_omni_rule_conflict"):
        classify_repo_file("omni/resources/config.yaml", rules)


@pytest.mark.parametrize("path", [
    "omni/model.yaml", "omni/relationships.yml", "omni/resources/orders.view.yaml",
    "omni/resources/orders.query.view", "omni/resources/sales.topic", "omni/resources/sales.composite_topic",
    "omni/resources/joins.relationships.yaml", ".github/workflows/ci.yml", ".omniflow.yml",
])
def test_explicit_source_or_control_exclusions_are_rejected(path):
    with pytest.raises(FileScopeError, match="non_omni_source_conflict"):
        parse_non_omni_rules([rule(path)])


def test_glob_cannot_hide_typed_source_or_control_file():
    for path, pattern in [
        ("omni/resources/orders.view.yaml", "omni/resources/*.yaml"),
        (".github/workflows/ci.yml", ".github/workflows/*.yml"),
    ]:
        with pytest.raises(FileScopeError, match="non_omni_source_conflict"):
            classify_repo_file(path, parse_non_omni_rules([rule(pattern)]))


def test_unknown_yaml_is_only_tagged_not_declared_absent_from_omni():
    rules = parse_non_omni_rules([rule()])
    assert classify_repo_file("omni/resources/config.yaml", rules) == "tagged_non_omni"
    assert classify_repo_file("omni/other.yaml", rules) == "unknown"
    assert classify_repo_file("omni/orders.view.yaml", rules) == "omni"
    with pytest.raises(FileScopeError, match="non_omni_source_conflict"):
        excluded_semantic_paths([], ["resources/config.yaml"], model_path="omni", rules=rules)


def test_root_model_patterns_remain_repository_relative():
    rules = parse_non_omni_rules([rule("resources/*.yaml")])
    assert excluded_semantic_paths(
        ["resources/config.yaml", "model"], ["model"], model_path=".", rules=rules,
    ) == {"resources/config.yaml"}
    assert excluded_semantic_paths(
        ["resources/config.yaml"], [], model_path="different", rules=rules,
    ) == set()


def test_pr_policy_cannot_classify_itself_using_head_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "synthetic@example.invalid")
    Path(".omniflow.yml").write_text("checks: {}\n")
    git("add", ".omniflow.yml")
    git("commit", "-m", "Trusted policy")
    base_sha = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", base_sha)
    git("switch", "-c", "candidate")
    Path(".omniflow.yml").write_text(json.dumps({"files": {"non_omni": [rule()]}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {
        "base": {"sha": base_sha, "repo": {"full_name": "test/models"}},
    }}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    assert load_config(".omniflow.yml").files.non_omni == []
