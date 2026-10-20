import json

import pytest

from omniflow.diagnostics import public_diagnostic
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


def test_duplicate_view_filenames_keep_exact_locations_but_never_guess_directory_aliases():
    files = {"sales/orders.view.yaml": VIEW, "marketing/orders.view.yaml": VIEW,
             "unique.topic": "name: unique\nbase_view: orders\n"}
    index = SnapshotIndex("omni", files)
    assert index.detail({"yaml_path": "sales/orders.view,dimensions,id,sql"})["line"] == 4
    assert index.detail({"file": "orders.view"}) == {}
    assert index.detail({"file": "arbitrary/orders.view"}) == {}
    assert index.detail({"field": "orders.id"}) == {}
    assert index.detail({"topic": "unique"})["file"] == "omni/unique.topic"


def test_verified_duplicate_view_aliases_use_canonical_names_and_original_git_lines():
    files = {"sales/orders.view.yaml": VIEW, "marketing/orders.view.yaml": VIEW}
    index = SnapshotIndex(
        "omni", files,
        api_aliases={"api/one/orders.view": "sales/orders.view.yaml",
                     "api/two/orders.view": "marketing/orders.view.yaml"},
        canonical_view_names={"sales__orders": "sales/orders.view.yaml",
                              "marketing__orders": "marketing/orders.view.yaml"},
    )
    first = index.detail({"yaml_path": "api/one/orders.view,dimensions,id,sql"})
    second = index.detail({"validator": "content", "field": "marketing__orders.id"})
    assert (first["file"], first["line"], first["field"]) == ("omni/sales/orders.view.yaml", 4, "sales__orders.id")
    assert (second["file"], second["line"]) == ("omni/marketing/orders.view.yaml", 3)
    assert index.detail({"file": "api/unknown/orders.view"}) == {}
    assert index.detail({"field": "orders.id"}) == {}
    assert "private_source_value" not in json.dumps([first, second])


@pytest.mark.parametrize("reference", ["schema/Orders.view", "schema/orders.view.yaml.yaml"])
def test_alias_matching_never_changes_filename_case_or_double_strips(reference):
    index = SnapshotIndex("omni", {"schema/orders.view.yaml": VIEW})
    assert index.detail({"file": reference}) == {}


@pytest.mark.parametrize("reference", ["orders.view", "SCHEMA/orders.view", "different/orders.view.yml"])
def test_directory_independent_alias_maps_back_to_original_git_location(reference):
    index = SnapshotIndex("omni", {"schema/orders.view.yaml": VIEW})
    detail = index.detail({"yaml_path": f"{reference},dimensions,id,sql"})
    assert (detail["file"], detail["line"]) == ("omni/schema/orders.view.yaml", 4)


@pytest.mark.parametrize("model_path,file,allowed", [
    ("omni/Snowflake Analytics", "orders.view.yaml", True),
    ("omni", "Private Folder/orders.view.yaml", True),
    ("omni", "mrn_TEST123.view.yaml", False),
])
def test_aliases_preserve_repository_only_safe_paths_and_reject_sensitive_patterns(model_path, file, allowed):
    index = SnapshotIndex(model_path, {file: VIEW})
    for issue in ({"file": file}, {"field": "orders.id"}):
        detail = index.detail(issue)
        if not allowed:
            assert detail == {}
            continue
        assert detail["file"] == f"{model_path}/{file}"
        assert "private_source_value" not in json.dumps(detail)
        public = public_diagnostic({"issues": [{"severity": "error", **detail}]})
        assert "file" not in public["issues"][0]
        assert f"{model_path}/{file}" not in json.dumps(public)


def test_generic_yaml_requires_exact_name_but_reserved_names_ignore_directories():
    index = SnapshotIndex("omni", {"settings.yaml": "description: private_source_value\n",
                                   "config/model.yaml": "topics:\n  sales:\n    base_view: orders\n"})
    assert index.detail({"file": "settings"}) == {}
    assert index.detail({"file": "model"})["file"] == "omni/config/model.yaml"
    assert index.detail({"file": "settings.yaml"})["file"] == "omni/settings.yaml"


@pytest.mark.parametrize("path", ["nested/relationships.yaml", "nested/relationships.yml", "nested/joins.relationships.yaml"])
def test_nested_relationship_lists_do_not_hide_verified_field_locations(path):
    index = SnapshotIndex("omni", {"schema/orders.view.yaml": VIEW, path: "[]\n"})
    detail = index.detail({"field": "orders.id"})
    assert detail["file"] == "omni/schema/orders.view.yaml"
    assert detail["line"] == 3
