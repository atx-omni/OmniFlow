"""Present catalog guidance on user-facing reporting surfaces."""
from __future__ import annotations

from typing import Any

from ..troubleshooting import guide_url


def diagnostic_guide(finding: dict[str, Any], report: dict[str, Any]) -> str:
    anchor = finding.get("guide_anchor")
    if not anchor:
        return ""
    revision = report.get("tool_revision") or {}
    return guide_url(anchor, revision.get("commit"), revision.get("repository")) if (
        revision.get("source") == "github_action_ref"
    ) else guide_url(anchor)


def first_step(finding: dict[str, Any]) -> str:
    steps = finding.get("recovery_steps")
    return steps[0] if steps else finding.get("next_action", "Inspect the failed check in this run.")


def mismatch_description(finding: dict[str, Any]) -> str:
    return finding.get("difference_description") or "A saved Omni file differs from its matching Git file."


def candidate_detail_hint(finding: dict[str, Any], report: dict[str, Any]) -> str:
    if finding.get("candidate_reason") != "content_mismatch":
        return ""
    availability = report.get("repository_diagnostics") or {}
    if availability.get("status") == "unavailable":
        reason = ("GitHub marks this repository internal. "
                  if availability.get("diagnostics_reason") == "repository_internal" else "")
        return reason + "No detailed artifact was generated for this run."
    if availability.get("candidate_summary_written") is True and report.get("diagnostic_audience") != "repository":
        return "Open this run’s Summary for the filename and line."
    return ""
