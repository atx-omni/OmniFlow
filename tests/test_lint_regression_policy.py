from omniflow.validators.yaml_lint import compare_lint_findings, has_error


def test_existing_lint_debt_does_not_defeat_new_only_policy():
    old = {"rule_id": "require_field_descriptions", "field": "orders.id", "severity": "error"}
    result = compare_lint_findings([old], [old])
    assert result[0]["state"] == "existing"
    assert not has_error(result)
    assert has_error(compare_lint_findings([old, old], [old]))
    assert has_error(compare_lint_findings([old], [dict(old, severity="warning")]))


def test_distinct_lint_error_cannot_match_on_count_or_message():
    old = {"rule_id": "require_field_descriptions", "field": "orders.id", "severity": "error"}
    new = dict(old, field="orders.total")
    assert has_error(compare_lint_findings([new], [old]))


def test_scope_security_policy_is_never_grandfathered():
    issue = {"rule_id": "forbid_personal_folder_validation_scope", "severity": "error"}
    assert has_error(compare_lint_findings([issue], [issue]))
