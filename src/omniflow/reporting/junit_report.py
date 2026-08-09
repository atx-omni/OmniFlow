from __future__ import annotations

from pathlib import Path
from typing import Any

# ElementTree is used only to serialize reports; no untrusted XML is parsed.
from xml.etree import ElementTree  # nosec B405


def to_junit(report: dict[str, Any]) -> str:
    issues = report.get("issues", [])
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": "omniflow",
            "tests": str(max(1, len(issues))),
            "failures": str(sum(1 for issue in issues if _is_failure(issue))),
        },
    )
    if not issues:
        ElementTree.SubElement(suite, "testcase", {"name": "omniflow"})
    for index, issue in enumerate(issues, start=1):
        case = ElementTree.SubElement(
            suite, "testcase", {"name": issue.get("rule_id") or issue.get("type") or f"issue-{index}"}
        )
        if _is_failure(issue):
            failure = ElementTree.SubElement(
                case, "failure", {"message": issue.get("message") or issue.get("summary") or "OmniFlow issue"}
            )
            failure.text = str(issue)
    return ElementTree.tostring(suite, encoding="unicode")


def write_junit_report(path: str | Path, report: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_junit(report) + "\n", encoding="utf-8")


def _is_failure(issue: dict[str, Any]) -> bool:
    return issue.get("active", True) and issue.get("severity") == "error"
