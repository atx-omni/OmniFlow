from __future__ import annotations

from pathlib import Path
from typing import Any


def render_markdown_report(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    decision = str(report.get("policy_decision") or "unknown")
    exit_reason = str(report.get("exit_code_reason") or "")
    issues = report.get("issues", [])
    blocking = [issue for issue in issues if _is_blocking(issue)]
    impacts = [issue for issue in issues if issue.get("validator") == "contracts" or issue.get("impact_level")]
    coverage_gaps = _coverage_gaps(report)
    lines = [
        "# OmniFlow",
        "",
        "## Decision",
        "",
        f"**{_decision_label(decision)}**",
        "",
        f"- Policy decision: `{decision}`",
        f"- Exit code reason: `{exit_reason}`",
        f"- Blocking issues: `{len(blocking)}`",
        f"- Downstream impacts: `{len(impacts)}`",
        f"- Coverage gaps: `{len(coverage_gaps)}`",
        "",
        "## Model Context",
        "",
        *_model_lines(report),
        "",
        "## Blocking Issues",
        "",
        *_issue_lines(blocking, empty="_No blocking issues._", limit=20),
        "",
        "## Downstream Contract Impact",
        "",
        *_impact_lines(impacts),
        "",
        "## Coverage Gaps",
        "",
        *_coverage_gap_lines(coverage_gaps),
        "",
        "## Validation Summary",
        "",
        f"- Total issues: `{summary.get('total_issues', 0)}`",
        f"- Errors: `{summary.get('errors', 0)}`",
        f"- Warnings: `{summary.get('warnings', 0)}`",
        f"- New issues: `{summary.get('new_issues', 0)}`",
        f"- Existing issues: `{summary.get('existing_issues', 0)}`",
        f"- Resolved issues: `{summary.get('resolved_issues', 0)}`",
        f"- Risk level: `{summary.get('risk_level', 'info')}`",
        "",
        "## Reviewer Actions",
        "",
        *_reviewer_actions(decision, blocking, impacts, coverage_gaps),
        "",
        "## Audit Metadata",
        "",
        f"- Tool version: `{report.get('tool_version', 'unknown')}`",
        f"- Generated at: `{report.get('generated_at', '')}`",
        f"- Git SHA: `{report.get('git_sha', '')}`",
        f"- Git branch: `{report.get('git_branch', '')}`",
        f"- Config hash: `{report.get('config_hash', '')}`",
    ]
    lines.append("")
    return "\n".join(lines)


def write_markdown_report(path: str | Path, report: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown_report(report), encoding="utf-8")


def _decision_label(decision: str) -> str:
    if decision == "pass":
        return "Pass: OmniFlow checks passed."
    if decision == "skipped":
        return "Skipped: no Omni semantic-layer changes were detected."
    if decision == "fail":
        return "Fail: review blocking issues before merge."
    return "Review required: OmniFlow could not determine a final decision."


def _model_lines(report: dict[str, Any]) -> list[str]:
    models = report.get("models")
    if isinstance(models, list) and models:
        lines = []
        for model in models:
            if not isinstance(model, dict):
                continue
            branch = model.get("branch_name") or model.get("branch_id") or ""
            lines.append(
                f"- `{model.get('model_id', '')}` path `{model.get('model_path', '')}` branch `{branch}`"
            )
        return lines or ["- Model context unavailable."]
    return [
        f"- Model ID: `{report.get('model_id', '')}`",
        f"- Model path: `{report.get('model_path', '')}`",
        f"- Branch: `{report.get('branch_name') or report.get('branch_id') or ''}`",
    ]


def _issue_lines(issues: list[dict[str, Any]], *, empty: str, limit: int) -> list[str]:
    if not issues:
        return [empty]
    lines = []
    for issue in issues[:limit]:
        severity = issue.get("severity") or issue.get("risk") or "info"
        location = issue.get("file") or issue.get("yaml_path") or issue.get("field") or issue.get("name") or ""
        lines.append(f"- **{severity}** `{location}` {issue.get('message') or issue.get('summary') or ''}")
    if len(issues) > limit:
        lines.append(f"- _{len(issues) - limit} more issue(s) omitted from PR summary._")
    return lines


def _impact_lines(impacts: list[dict[str, Any]]) -> list[str]:
    if not impacts:
        return ["_No downstream contract impacts._"]
    lines = []
    for issue in impacts[:20]:
        impact_level = issue.get("impact_level") or "unknown"
        target = issue.get("field") or issue.get("previous_field") or issue.get("name") or ""
        referenced = issue.get("referenced_content") if isinstance(issue.get("referenced_content"), list) else []
        lines.append(f"- **{impact_level}** `{target}` referenced content: `{len(referenced)}`")
    if len(impacts) > 20:
        lines.append(f"- _{len(impacts) - 20} more impact(s) omitted from PR summary._")
    return lines


def _coverage_gaps(report: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []
    for key in ("coverage_gaps", "dependency_coverage_gaps"):
        value = report.get(key)
        if isinstance(value, list):
            gaps.extend(item for item in value if isinstance(item, dict))
    for model_report in report.get("model_reports", []) if isinstance(report.get("model_reports"), list) else []:
        if not isinstance(model_report, dict):
            continue
        for check_report in model_report.get("check_reports", []) if isinstance(model_report.get("check_reports"), list) else []:
            value = check_report.get("coverage_gaps") if isinstance(check_report, dict) else None
            if isinstance(value, list):
                gaps.extend(item for item in value if isinstance(item, dict))
    return gaps


def _coverage_gap_lines(gaps: list[dict[str, Any]]) -> list[str]:
    if not gaps:
        return ["_No dependency coverage gaps._"]
    lines = []
    for gap in gaps[:10]:
        lines.append(f"- `{gap.get('type', '')}` `{gap.get('name', '')}` {gap.get('message', '')}")
    if len(gaps) > 10:
        lines.append(f"- _{len(gaps) - 10} more coverage gap(s) omitted from PR summary._")
    return lines


def _reviewer_actions(
    decision: str,
    blocking: list[dict[str, Any]],
    impacts: list[dict[str, Any]],
    coverage_gaps: list[dict[str, Any]],
) -> list[str]:
    if decision == "pass":
        actions = ["- Review semantic diff and downstream impact artifacts before approving."]
        if coverage_gaps:
            actions.append("- Review dependency coverage gaps before treating impact analysis as complete.")
        return actions
    if decision == "skipped":
        return ["- No reviewer action needed for OmniFlow unless this PR was expected to contain Omni changes."]
    actions = []
    if blocking:
        actions.append("- Resolve blocking validation, lint, or contract issues before merge.")
    if impacts:
        actions.append("- Review referenced dashboards, reports, and queries before approving semantic changes.")
    if coverage_gaps:
        actions.append("- Re-run or inspect dependency coverage gaps; impact analysis may be incomplete.")
    return actions or ["- Review OmniFlow artifacts for setup or configuration errors."]


def _is_blocking(issue: dict[str, Any]) -> bool:
    return issue.get("severity") == "error" or issue.get("impact_level") == "referenced_breaking"
