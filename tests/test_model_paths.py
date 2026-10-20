import pytest

from omniflow.discovery import _is_probable_omni_file, _is_under_model_path
from omniflow.model_paths import (
    AmbiguousModelPath,
    authored_file_identity,
    identity_index,
    is_git_model_file,
    is_semantic_file,
)


@pytest.mark.parametrize("path,expected", [
    ("model.yaml", "model"), ("relationships.yml", "relationships"),
    ("SCHEMA/orders.view.yaml", "SCHEMA/orders.view"),
    ("Snowflake Analytics/Orders.query.view.yml", "Snowflake Analytics/Orders.query.view"),
    ("Sales.topic.yaml", "Sales.topic"), ("Sales.composite_topic.yml", "Sales.composite_topic"),
    ("nested/orders.relationships.yaml", "nested/orders.relationships"),
    ("orders.view", "orders.view"), ("settings.yaml", "settings.yaml"),
    ("nested/model.yaml", "nested/model.yaml"), ("Model.yaml", "Model.yaml"),
    ("model.yaml.yaml", "model.yaml.yaml"), ("orders.view.yml.yaml", "orders.view.yml.yaml"),
])
def test_only_one_recognized_wrapper_is_removed(path, expected):
    assert authored_file_identity(path) == expected


@pytest.mark.parametrize("paths", [
    ["orders.view", "orders.view.yaml"], ["orders.view.yaml", "orders.view.yml"],
    ["model", "model.yml"], ["model.yaml", "model.yaml"],
])
def test_collisions_are_rejected_without_disclosing_paths(paths):
    with pytest.raises(AmbiguousModelPath) as caught:
        identity_index(paths)
    assert all(path not in str(caught.value) for path in paths)


def test_full_paths_case_and_spaces_are_preserved():
    paths = ["a/orders.view.yaml", "b/orders.view.yml", "a/Orders.view", "A Space/orders.view.yaml"]
    index = identity_index(paths)
    assert len(index) == 4
    assert index["a/orders.view"] == paths[0]
    assert index["A Space/orders.view"] == paths[-1]


@pytest.mark.parametrize("path", ["SCHEMA/orders.view.yaml", "A Space/orders.query.view.yml", "Sales.topic.yaml"])
def test_root_model_routing_recognizes_native_typed_exports(path):
    assert _is_probable_omni_file(path)
    assert _is_under_model_path(path, ".")


@pytest.mark.parametrize("path", ["SCHEMA/settings.yaml", "SCHEMA/orders.view.yaml.yaml"])
def test_root_routing_does_not_guess_unknown_yaml(path):
    assert not _is_probable_omni_file(path)


@pytest.mark.parametrize("path", [
    "model", "relationships", "joins.relationships", "joins.relationships.yaml", "joins.relationships.yml",
    "SCHEMA/orders.query.view", "A Space/orders.view.yaml", "sales.composite_topic", "nested/model.yaml",
])
def test_candidate_and_analysis_use_the_same_file_classifier(path):
    from pathlib import Path

    from omniflow.candidate import _semantic_path
    from omniflow.diff.yaml_loader import _is_yaml_model_file

    assert is_semantic_file(path)
    assert _semantic_path(path) == _is_yaml_model_file(Path(path))


@pytest.mark.parametrize("path", [
    "model", "model.yaml", "model.yml", "relationships", "relationships.yaml", "relationships.yml",
    "SCHEMA/orders.view", "SCHEMA/orders.view.yaml", "A Space/orders.query.view.yml",
    "sales.topic", "sales.topic.yaml", "sales.composite_topic.yml", "joins.relationships.yaml",
])
def test_git_defaults_only_recognize_omni_families(path):
    assert is_git_model_file(path)


@pytest.mark.parametrize("path", [
    "README.md", "query.sql", "data.csv", "metadata.json", ".DS_Store", "settings.yaml", "schema.yml",
    "views/orders.yaml", "topics/tooling.yml", "nested/model.yaml", "nested/relationships",
    "Model.yaml", "orders.VIEW.yaml", "orders.view.yaml.yaml",
])
def test_unrelated_git_files_are_not_model_sources(path):
    assert not is_git_model_file(path)
