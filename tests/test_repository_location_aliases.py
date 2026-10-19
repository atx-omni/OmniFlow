import json

import pytest

from omniflow.repository_locations import SnapshotIndex

VIEW = "name: orders\ndimensions:\n  id:\n    sql: private_source_value\n"


@pytest.mark.parametrize("git_suffix", ["", ".yaml", ".yml"])
@pytest.mark.parametrize("api_suffix", ["", ".yaml", ".yml"])
@pytest.mark.parametrize("qualified", [False, True])
def test_supported_aliases_resolve_to_original_git_source(git_suffix, api_suffix, qualified):
    source = f"schema/orders.view{git_suffix}"
    reference = ("omni/" if qualified else "") + f"schema/orders.view{api_suffix}"
    index = SnapshotIndex("omni", {source: VIEW})
    detail = index.detail({"validator": "model", "yaml_path": f"{reference},dimensions,id,sql"})
    assert detail["file"] == f"omni/{source}"
    assert detail["line"] == 4
    assert "private_source_value" not in json.dumps(detail)


@pytest.mark.parametrize("suffix", [".yaml", ".yml"])
@pytest.mark.parametrize("reference", ["model", "model.yml", "omni/model", "omni/model.yml"])
def test_root_model_alias_preserves_original_location(suffix, reference):
    index = SnapshotIndex("omni", {f"model{suffix}": "topics:\n  sales:\n    base_view: orders\n"})
    detail = index.detail({"validator": "model", "yaml_path": f"{reference},topics,sales,base_view"})
    assert detail["file"] == f"omni/model{suffix}" and detail["line"] == 3


@pytest.mark.parametrize("other", ["orders.view", "orders.view.yml"])
def test_serialization_collisions_disable_all_location_guessing(other):
    index = SnapshotIndex("omni", {"orders.view.yaml": VIEW, other: VIEW})
    assert index.detail({"file": "orders.view.yaml"}) == {}
    assert index.detail({"field": "orders.id"}) == {}


def test_scoped_aliases_do_not_hide_prefixed_path_ambiguity():
    index = SnapshotIndex("omni", {"orders.view.yaml": VIEW, "omni/orders.view.yaml": VIEW})
    assert index.detail({"file": "omni/orders.view"}) == {}
    assert index.detail({"file": "omni/orders.view.yml"}) == {}


@pytest.mark.parametrize("reference", ["orders.view", "SCHEMA/orders.view", "schema/orders.view.yaml.yaml"])
def test_alias_matching_never_changes_case_discards_directories_or_double_strips(reference):
    index = SnapshotIndex("omni", {"schema/orders.view.yaml": VIEW})
    assert index.detail({"file": reference}) == {}


@pytest.mark.parametrize("model_path,file", [
    ("omni/Snowflake Analytics", "orders.view.yaml"),
    ("omni", "Private Folder/orders.view.yaml"),
    ("omni", "mrn_TEST123.view.yaml"),
])
def test_aliases_do_not_expand_existing_path_disclosure_policy(model_path, file):
    index = SnapshotIndex(model_path, {file: VIEW})
    assert index.detail({"file": file}) == {}
    assert index.detail({"field": "orders.id"}) == {}


def test_generic_and_nested_special_yaml_are_not_invented_api_aliases():
    index = SnapshotIndex("omni", {"settings.yaml": "description: private_source_value\n",
                                   "config/model.yaml": "topics:\n  sales:\n    base_view: orders\n"})
    assert index.detail({"file": "settings"}) == {}
    assert index.detail({"file": "config/model"}) == {}
    assert index.detail({"file": "settings.yaml"})["file"] == "omni/settings.yaml"
