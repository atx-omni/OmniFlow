from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from ..artifacts import public_dir, write_public_json
from ..security import public_safe, secure_write_text


def write_repair_artifacts(
    report: dict[str, Any], *, output_dir: str | Path, redaction_level: str
) -> dict[str, Any]:
    root = Path(output_dir)
    safe_report = write_public_json(root / "repair.json", report, redaction_level=redaction_level)
    write_public_json(public_dir(root) / "repair.json", report, redaction_level=redaction_level)
    markdown = render_repair_markdown(safe_report)
    secure_write_text(root / "repair.md", markdown)
    secure_write_text(public_dir(root) / "repair.md", markdown)
    evidence = {
        key: safe_report.get(key)
        for key in (
            "tool",
            "tool_version",
            "operation",
            "generated_at",
            "status",
            "model_id",
            "branch_id",
            "branch_name",
            "pull_request_number",
            "head_sha",
            "config_hash",
            "query_execution_acknowledged",
            "raw_query_results_stored",
            "change_summary",
            "full_validation",
            "rollback",
            "commit",
            "manual_review_required",
            "policy_decision",
            "exit_code",
            "exit_code_reason",
            "reason_code",
            "cause_code",
        )
    }
    write_public_json(root / "evidence.json", evidence, redaction_level=redaction_level)
    write_public_json(public_dir(root) / "evidence.json", evidence, redaction_level=redaction_level)
    return safe_report


def render_repair_markdown(report: dict[str, Any]) -> str:
    status = _safe_text(report.get("status") or "unknown")
    message = _safe_text(report.get("message") or "")
    lines = [
        "# OmniFlow AI Repair",
        "",
        f"**Status:** `{status}`",
        f"**Policy decision:** `{_safe_text(report.get('policy_decision') or 'fail')}`",
        "Resource identities and authored text are omitted from shareable diagnostics.",
        f"**Run reference:** `{_safe_text(report.get('run_reference') or 'unavailable')}`",
        "",
        message,
        "",
    ]
    findings = report.get("issues", [])
    if findings:
        lines.extend(["## Failure and Recovery", ""])
        for issue in findings[:20]:
            code = _safe_text(issue.get("diagnostic_code") or "OF-AI-REPAIR")
            lines.append(f"- **{code}**: {_safe_text(issue.get('message') or 'Repair did not establish success.')}")
            if issue.get("next_action"):
                lines.append(f"  - Next: {_safe_text(issue['next_action'])}")
            for key in ("reason_code", "cause_code", "operation_phase", "api_operation", "http_status"):
                if key in issue:
                    lines.append(f"  - {key}: `{_safe_text(issue[key])}`")
    change = report.get("change_summary") if isinstance(report.get("change_summary"), dict) else {}
    modified = change.get("modified_files") if isinstance(change.get("modified_files"), list) else []
    lines.extend(["## Reviewed Changes", ""])
    if change.get("modified_file_count"):
        lines.append(f"- Modified file count: `{change['modified_file_count']}`")
        lines.append(f"- Changed lines: `{int(change.get('changed_lines') or 0)}`")
    elif modified:
        lines.extend(f"- `{_safe_text(file_name)}`" for file_name in modified)
        lines.append(f"- Changed lines: `{int(change.get('changed_lines') or 0)}`")
    else:
        lines.append("No authored-YAML change was accepted.")
    validation = report.get("full_validation")
    if isinstance(validation, dict) and validation.get("failed_finding_count"):
        lines.extend(["", "## Failed Validation", ""])
        lines.append(f"- Failed findings: `{int(validation['failed_finding_count'])}`")
        for issue in validation.get("issues", [])[:20]:
            if not isinstance(issue, dict):
                continue
            validator = _safe_text(issue.get("validator") or "validation")
            reason = _safe_text(issue.get("diagnostic_code") or issue.get("reason_code") or issue.get("rule_id") or "unknown")
            action = _safe_text(issue.get("next_action") or "Inspect the matching validation report before retrying repair.")
            lines.append(f"- `{validator}` / `{reason}`: {action}")
    lines.extend(["", "## Safety", ""])
    lines.append("- Raw query results stored by OmniFlow: `false`")
    lines.append(f"- Rollback verified: `{bool((report.get('rollback') or {}).get('verified'))}`")
    lines.append(f"- Manual review required: `{bool(report.get('manual_review_required'))}`")
    lines.append("")
    lines.append("OmniFlow does not approve, merge, or deploy AI repairs. Review the pull request diff before merge.")
    return "\n".join(lines) + "\n"


def _safe_text(value: Any) -> str:
    safe = public_safe(str(value), redaction_level="standard")
    return html.escape(str(safe), quote=True).replace("`", "'").replace("@", "&#64;")
