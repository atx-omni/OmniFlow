import pytest

from omniflow.artifact_identity import ArtifactIdentityError, identity_digest, match_artifacts


def physical(schema="sales", catalog="warehouse", table="orders"):
    return (f"catalog: {catalog}\n" if catalog is not None else "") + f"schema: {schema}\ntable_name: {table}\n"


def test_identity_digest_is_order_independent_and_contains_no_plaintext():
    pairs = {"Review/orders.view.yaml": "SCHEMA/orders.view", "Topics/sales.topic": "sales.topic"}
    names = {"schema__orders": "SCHEMA/orders.view", "archive__orders": "ARCHIVE/orders.view"}
    actual = identity_digest(pairs, omni_view_names=names)
    assert actual == identity_digest(dict(reversed(pairs.items())), omni_view_names=dict(reversed(names.items())))
    assert len(actual) == 64 and all(char in "0123456789abcdef" for char in actual)


def test_identity_digest_changes_with_canonical_identity_even_when_file_pairs_do_not():
    pairs = {"orders.view": "orders.view"}
    assert identity_digest(pairs, omni_view_names={"orders": "orders.view"}) != identity_digest(
        pairs, omni_view_names={"sales__orders": "orders.view"},
    )


@pytest.mark.parametrize("moved", [{"Moved/orders.view": "orders.view"}, {"orders.view": "Moved/orders.view"}])
def test_identity_digest_binds_both_sides_original_paths(moved):
    assert identity_digest({"orders.view": "orders.view"}) != identity_digest(moved)


def test_identity_digest_preserves_missing_metadata_and_namespace_boundaries():
    pairs = {"orders.view": "orders.view"}
    assert identity_digest(pairs) != identity_digest(pairs, omni_view_names={})
    assert identity_digest({"a/b": "c"}) != identity_digest({"a": "b/c"})


def test_exact_paths_allow_duplicate_view_basenames_without_parsing_invalid_yaml():
    git = {"A/orders.view.yaml": "invalid: [", "B/orders.view.yml": "also invalid: ["}
    api = {"A/orders.view": "changed bytes", "B/orders.view": "different bytes"}
    result = match_artifacts(git, api)
    assert result.pairs == {"A/orders.view.yaml": "A/orders.view", "B/orders.view.yml": "B/orders.view"}
    assert set(result.pairing_methods.values()) == {"authored_path"}
    assert not result.unmatched_git and not result.unmatched_omni


@pytest.mark.parametrize("qualifiers", [
    [("one", "warehouse"), ("two", "warehouse")],
    [("sales", "db_one"), ("sales", "db_two")],
    [("sales", None), ("sales", "warehouse")],
])
def test_qualified_view_fallback_preserves_database_schema_and_default_catalog(qualifiers):
    texts = [physical(schema, catalog) for schema, catalog in qualifiers]
    git = {f"Review Folder/{index}/orders.view.yaml": text for index, text in enumerate(texts)}
    api = {f"Native/{index}/orders.view": text for index, text in enumerate(texts)}
    result = match_artifacts(git, api)
    assert result.pairs == {f"Review Folder/{i}/orders.view.yaml": f"Native/{i}/orders.view" for i in range(2)}
    assert set(result.pairing_methods.values()) == {"qualified_view"}


def test_singletons_topics_and_named_relationships_match_across_folders():
    names = ["model", "relationships", "sales.topic", "sales.composite_topic", "joins.relationships"]
    git = {f"A Space/{name}.yaml": "invalid YAML [" for name in names}
    api = dict.fromkeys(names, "different content is not identity")
    result = match_artifacts(git, api)
    assert result.pairs == {f"A Space/{name}.yaml": name for name in names}
    assert result.pairing_methods["A Space/sales.topic.yaml"] == "topic_name"
    assert result.pairing_methods["A Space/joins.relationships.yaml"] == "typed_relationship_name"


@pytest.mark.parametrize("name", ["model", "relationships", "sales.topic", "sales.composite_topic", "x.relationships"])
@pytest.mark.parametrize("side", ["git", "omni"])
def test_duplicate_singleton_or_named_artifact_is_rejected_even_if_exact_and_identical(name, side):
    duplicate = {f"a/{name}": "same", f"b/{name}.yaml": "same"}
    git, api = (duplicate, duplicate) if side == "git" else ({}, duplicate)
    with pytest.raises(ArtifactIdentityError) as caught:
        match_artifacts(git, api)
    assert caught.value.reason == "ambiguous_file_identity"
    assert caught.value.identity_reason.startswith("duplicate_")
    assert not any(path in str(caught.value) for path in duplicate)


def test_reserved_names_with_view_suffix_are_views_not_singletons():
    git = {"a/model.view.yaml": "{}", "b/model.view.yaml": "{}", "relationships.view.yml": "{}"}
    api = {"a/model.view": "{}", "b/model.view": "{}", "relationships.view": "{}"}
    assert len(match_artifacts(git, api).pairs) == 3


def test_api_confirmed_generic_yaml_remains_independent_of_payload_type_hints():
    git = {"nested/settings.yaml": "type: model", "nested/more.yml": "type: model"}
    api = {"settings.yaml": "type: model", "nested/more.yml": "type: model"}
    result = match_artifacts(git, api)
    assert len(result.pairs) == 2
    assert result.pairing_methods["nested/settings.yaml"] == "unique_generic_filename"


@pytest.mark.parametrize("git_path", ["native/orders.yaml", "Review/orders.yaml"])
def test_normalized_generic_api_view_metadata_preserves_exact_and_unique_name_matching(git_path):
    result = match_artifacts(
        {git_path: "dimensions: {}"}, {"native/orders.yaml": "dimensions: {}"},
        omni_view_names={"schema__orders": "native/orders.yaml"},
    )
    assert result.pairs == {git_path: "native/orders.yaml"}
    assert result.pairing_methods[git_path] in {"authored_path", "unique_generic_filename"}


@pytest.mark.parametrize("git_path", ["schema__orders.yaml", "schema__orders.view"])
def test_generic_api_canonical_metadata_does_not_invent_git_filename_aliases(git_path):
    result = match_artifacts(
        {git_path: "dimensions: {}"}, {"orders.yaml": "dimensions: {}"},
        omni_view_names={"schema__orders": "orders.yaml"},
    )
    assert not result.pairs
    assert result.unmatched_git == {git_path}
    assert result.unmatched_omni == {"orders.yaml"}


def test_unique_query_view_does_not_require_warehouse_qualification():
    result = match_artifacts({"Moved/orders.query.view.yaml": "sql: select 1"}, {"orders.query.view": "sql: select 2"})
    assert result.pairs == {"Moved/orders.query.view.yaml": "orders.query.view"}
    assert result.pairing_methods["Moved/orders.query.view.yaml"] == "unique_view_filename"


@pytest.mark.parametrize("filename", ["schema__orders.view.yaml", "schema__orders.query.view.yml"])
def test_full_canonical_view_filename_can_match_authoritative_api_identity(filename):
    result = match_artifacts({f"Moved/{filename}": "{}"}, {"SCHEMA/orders.view": "{}"},
                             omni_view_names={"schema__orders": "SCHEMA/orders.view"})
    assert result.pairs == {f"Moved/{filename}": "SCHEMA/orders.view"}
    assert set(result.pairing_methods.values()) == {"canonical_view_name"}


def test_canonical_match_never_guesses_underscore_normalization():
    result = match_artifacts({"schema_orders.view": "{}"}, {"SCHEMA/orders.view": "{}"},
                             omni_view_names={"schema__orders": "SCHEMA/orders.view"})
    assert not result.pairs
    assert result.unmatched_git == {"schema_orders.view"}
    assert result.unmatched_omni == {"SCHEMA/orders.view"}


def test_alias_views_on_same_physical_table_stay_distinct_by_authored_name():
    text = physical()
    result = match_artifacts({"Moved/buyers.view": text, "Moved/sellers.view": text},
                             {"buyers.view": text, "sellers.view": text})
    assert result.pairs == {"Moved/buyers.view": "buyers.view", "Moved/sellers.view": "sellers.view"}
    unmatched = match_artifacts({"buyers.view": text}, {"sellers.view": text})
    assert not unmatched.pairs


@pytest.mark.parametrize("text", [
    "dimensions: {}", physical(), "invalid: [", physical() + "schema: another\n",
    physical() + "sql: select 1\n", physical() + "query: {}\n",
])
def test_ambiguous_moved_same_basename_is_not_paired_by_contents_or_residual_order(text):
    # One pair is exact. The remaining pair is still globally ambiguous.
    git = {"same/orders.view": text, "Moved/orders.view": text}
    api = {"same/orders.view": text, "Other/orders.view": text}
    with pytest.raises(ArtifactIdentityError) as caught:
        match_artifacts(git, api)
    assert caught.value.identity_reason == "ambiguous_view_correspondence"


def test_distinct_bytes_do_not_disambiguate_missing_physical_evidence():
    git = {"a/orders.view": "label: first", "b/orders.view": "label: second"}
    api = {"x/orders.view": "label: first", "y/orders.view": "label: second"}
    with pytest.raises(ArtifactIdentityError):
        match_artifacts(git, api)


def test_exact_unreadable_sibling_cannot_be_removed_to_make_physical_identity_unique():
    git = {"same/orders.view": "invalid: [", "Moved/orders.view": physical()}
    api = {"same/orders.view": "invalid: [", "Other/orders.view": physical()}
    with pytest.raises(ArtifactIdentityError):
        match_artifacts(git, api)


def test_default_catalog_is_not_wildcard_for_an_explicit_catalog():
    git = {"a/orders.view": physical("one", None), "b/orders.view": physical("two", None)}
    api = {"x/orders.view": physical("one", "warehouse"), "y/orders.view": physical("two", "warehouse")}
    with pytest.raises(ArtifactIdentityError):
        match_artifacts(git, api)


@pytest.mark.parametrize("path", ["Orders.view", "orders.VIEW", "orders.view.yaml.yaml"])
def test_case_and_double_wrapper_are_not_normalized(path):
    result = match_artifacts({path: "same bytes"}, {"orders.view": "same bytes"})
    assert not result.pairs
    assert result.unmatched_git == {path}


def test_missing_and_extra_unique_sources_are_returned_for_candidate_inventory_errors():
    result = match_artifacts({"old.topic": "{}"}, {"new.topic": "{}"})
    assert result.unmatched_git == {"old.topic"}
    assert result.unmatched_omni == {"new.topic"}


def test_wrapper_collision_is_rejected_and_structured_evidence_is_bounded():
    git = {f"folder/{name}": "{}" for name in ["model", "model.yaml", "model.yml"]}
    with pytest.raises(ArtifactIdentityError) as caught:
        match_artifacts(git, {})
    assert caught.value.identity_reason == "duplicate_authored_path"
    assert caught.value.candidate_count == 3
    assert len(caught.value.candidates) <= 5
    assert {candidate["git_path"] for candidate in caught.value.candidates} == set(git)


def test_private_candidate_samples_remain_bounded_for_large_collision_group():
    git = {f"{index}/sales.topic": "{}" for index in range(12)}
    with pytest.raises(ArtifactIdentityError) as caught:
        match_artifacts(git, {})
    assert caught.value.candidate_count == 12
    assert len(caught.value.candidates) == 5


def test_canonical_and_unique_filename_evidence_cannot_disagree():
    git = {"Moved/orders.view": "{}"}
    api = {"Native/orders.view": "{}", "Other/custom.view": "{}"}
    with pytest.raises(ArtifactIdentityError) as caught:
        match_artifacts(git, api, omni_view_names={"orders": "Other/custom.view", "other": "Native/orders.view"})
    assert caught.value.identity_reason == "conflicting_view_correspondence"


@pytest.mark.parametrize("mapping", [[], {"": "orders.view"}, {"orders": []}, {"a": "orders.view", "b": "orders.view"}])
def test_malformed_canonical_map_fails_with_fixed_safe_error(mapping):
    with pytest.raises(ArtifactIdentityError) as caught:
        match_artifacts({"orders.view": "{}"}, {"orders.view": "{}"}, omni_view_names=mapping)
    assert caught.value.identity_reason == "invalid_canonical_view_map"


@pytest.mark.parametrize("path", ["model", "relationships", "sales.topic", "legacy.relationships"])
def test_canonical_map_still_rejects_known_nonview_types(path):
    with pytest.raises(ArtifactIdentityError) as caught:
        match_artifacts({path: "{}"}, {path: "{}"}, omni_view_names={"not_a_view": path})
    assert caught.value.identity_reason == "invalid_canonical_view_map"
