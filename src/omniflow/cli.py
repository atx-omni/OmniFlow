from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .artifacts import public_dir, restricted_dir, write_artifact_manifest, write_public_json, write_public_reports
from .config import load_config, require_api_key
from .contracts import evaluate_contracts
from .diff.diff_engine import diff_graphs
from .diff.semantic_graph import build_graph
from .diff.yaml_loader import load_yaml_files
from .discovery import ModelContext, discover_contexts
from .downstream import generate_downstream_dependencies
from .evidence import build_evidence
from .exceptions import ConfigError, ExitCodes, OmniCIError
from .exposures import run_dbt_exposure_enrichment
from .git import current_branch, current_sha, pr_number
from .logging import configure_logging
from .omni_client import OmniClient
from .reporting.json_report import write_json_report
from .reporting.writer import write_reports
from .security import redact
from .validators.content import run_content_validation
from .validators.model import run_model_validation
from .validators.yaml_lint import has_error, lint_graph
from .yaml_pull import pull_yaml


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging("DEBUG" if getattr(args, "verbose", False) else "INFO")
    try:
        return args.func(args)
    except OmniCIError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        print(redact(f"Internal omniflow error: {exc}"), file=sys.stderr)
        return ExitCodes.INTERNAL_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omniflow", description="OmniFlow semantic-layer CI/CD orchestrator")
    parser.add_argument("--version", action="version", version=f"omniflow {__version__}")
    parser.add_argument("--verbose", action="store_true")
    subcommands = parser.add_subparsers(required=True)

    run_parser = subcommands.add_parser("run", help="Run enabled configured checks")
    _add_config_arg(run_parser)
    _add_common_omni_args(run_parser)
    run_parser.add_argument("--auto", action="store_true", help="Discover Omni model context from Omni-managed metadata")
    run_parser.set_defaults(func=cmd_run)

    content = subcommands.add_parser("content", help="Content validation commands")
    content_sub = content.add_subparsers(required=True)
    content_validate = content_sub.add_parser("validate", help="Validate Omni content")
    _add_config_arg(content_validate)
    _add_common_omni_args(content_validate)
    content_validate.add_argument("--history-in", default=None)
    content_validate.add_argument("--history-out", default=None)
    content_validate.add_argument("--report-out", default=None)
    content_validate.add_argument("--label", action="append", default=[])
    content_validate.add_argument("--labels", action="append", default=[])
    content_validate.add_argument("--fail-on-new-only", action=argparse.BooleanOptionalAction, default=None)
    content_validate.set_defaults(func=cmd_content_validate)

    model = subcommands.add_parser("model", help="Model validation commands")
    model_sub = model.add_subparsers(required=True)
    model_validate = model_sub.add_parser("validate", help="Validate Omni model YAML")
    _add_config_arg(model_validate)
    _add_common_omni_args(model_validate)
    model_validate.add_argument("--fail-on-warnings", action=argparse.BooleanOptionalAction, default=None)
    model_validate.set_defaults(func=cmd_model_validate)

    yaml_parser = subcommands.add_parser("yaml", help="YAML commands")
    yaml_sub = yaml_parser.add_subparsers(required=True)
    yaml_pull = yaml_sub.add_parser("pull", help="Fetch Omni model YAML")
    _add_config_arg(yaml_pull)
    _add_common_omni_args(yaml_pull)
    yaml_pull.add_argument("--out", default=None)
    yaml_pull.add_argument("--mode", default="combined")
    yaml_pull.add_argument("--fully-resolved", action="store_true")
    yaml_pull.set_defaults(func=cmd_yaml_pull)

    exposures = subcommands.add_parser("exposures", help="dbt exposure commands")
    exposures_sub = exposures.add_subparsers(required=True)
    exposures_pull = exposures_sub.add_parser("pull", help="Fetch Omni dbt exposure metadata")
    _add_config_arg(exposures_pull)
    _add_common_omni_args(exposures_pull)
    exposures_pull.add_argument("--out", default=None)
    exposures_pull.set_defaults(func=cmd_exposures_pull)

    diff_parser = subcommands.add_parser("diff", help="Compare semantic YAML")
    diff_parser.add_argument("--base", required=True, help="Directory containing base YAML")
    diff_parser.add_argument("--head", required=True, help="Directory containing head YAML")
    diff_parser.add_argument("--report-out", default=None)
    diff_parser.set_defaults(func=cmd_diff)

    report_parser = subcommands.add_parser("report", help="Render reports from report.json")
    report_parser.add_argument("--input", default=".omniflow/report.json")
    report_parser.add_argument("--output-dir", default=".omniflow")
    report_parser.add_argument("--format", action="append", default=["markdown"])
    report_parser.set_defaults(func=cmd_report)

    doctor = subcommands.add_parser("doctor", help="Check local configuration and environment")
    _add_config_arg(doctor)
    _add_common_omni_args(doctor)
    doctor.add_argument("--auto", action="store_true", help="Validate Omni-managed metadata discovery")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None)


def _add_common_omni_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url")
    parser.add_argument("--model-id")
    parser.add_argument("--model-path")
    parser.add_argument("--branch-id")
    parser.add_argument("--branch-name")
    parser.add_argument("--user-id")
    parser.add_argument("--include-personal-folders", action=argparse.BooleanOptionalAction, default=None)


def cmd_run(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    output_dir = Path(config.reporting.output_dir)
    try:
        contexts = discover_contexts(
            auto=args.auto,
            base_url=config.omni.base_url,
            model_id=config.omni.model_id,
            model_path=getattr(args, "model_path", None),
            branch_name=config.omni.branch_name,
            branch_id=config.omni.branch_id,
            allow_skip=True,
        )
    except OmniCIError as exc:
        _write_setup_failure_artifacts(config=config, output_dir=output_dir, exc=exc)
        raise
    if not contexts:
        _write_skipped_artifacts(config=config, output_dir=output_dir)
        print("OmniFlow skipped: no Omni PR context or changed Omni model files detected")
        return 0
    all_reports = []
    all_issues: list[dict[str, Any]] = []
    exit_code = 0
    for context in contexts:
        context_output_dir = restricted_dir(output_dir) / _safe_context_dir(context)
        try:
            context_report, context_exit = _run_context(
                config=config,
                context=context,
                output_dir=context_output_dir,
            )
        except OmniCIError as exc:
            context_report, context_exit = _write_context_failure_artifacts(
                config=config,
                context=context,
                output_dir=context_output_dir,
                exc=exc,
            )
        if not config.security.retain_restricted_artifacts and context_output_dir.exists():
            shutil.rmtree(context_output_dir)
        all_reports.append(context_report)
        all_issues.extend(context_report.get("issues", []))
        exit_code = max(exit_code, context_exit)

    summary = _summarize(all_issues)
    report = _aggregate_report(config, contexts, exit_code, all_issues, summary, all_reports)
    write_public_reports(
        report,
        output_dir=output_dir,
        formats=config.reporting.formats,
        redaction_level=config.security.redaction_level,
    )
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "config_hash": config.hash,
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "models": [_context_dict(context) for context in contexts],
        "validation_status": "failed" if exit_code else "passed",
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
        "timestamp": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_public_json(public_dir(output_dir) / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
        redaction_level=config.security.redaction_level,
    )
    print(f"OmniFlow complete: models={len(contexts)} issues={summary['total_issues']} exit_code={exit_code}")
    return exit_code


def _write_setup_failure_artifacts(*, config, output_dir: Path, exc: OmniCIError) -> None:
    issue = {
        "severity": "error",
        "validator": "setup",
        "message": redact(str(exc)),
    }
    summary = _summarize([issue])
    report = {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "models": [],
        "config_hash": config.hash,
        "summary": summary,
        "issues": [issue],
        "model_reports": [],
        "policy_decision": "fail",
        "exit_code": exc.exit_code,
        "exit_code_reason": _exit_code_reason(exc.exit_code),
    }
    write_public_reports(
        report,
        output_dir=output_dir,
        formats=config.reporting.formats,
        redaction_level=config.security.redaction_level,
    )
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "config_hash": config.hash,
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "models": [],
        "validation_status": "failed",
        "policy_decision": "fail",
        "exit_code": exc.exit_code,
        "exit_code_reason": report["exit_code_reason"],
        "timestamp": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_public_json(public_dir(output_dir) / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
        redaction_level=config.security.redaction_level,
    )


def _write_skipped_artifacts(*, config, output_dir: Path) -> None:
    summary = _summarize([])
    report = {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "models": [],
        "config_hash": config.hash,
        "summary": summary,
        "issues": [],
        "model_reports": [],
        "policy_decision": "skipped",
        "exit_code": 0,
        "exit_code_reason": "no Omni PR context or changed Omni model files detected",
    }
    write_public_reports(
        report,
        output_dir=output_dir,
        formats=config.reporting.formats,
        redaction_level=config.security.redaction_level,
    )
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "config_hash": config.hash,
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "models": [],
        "validation_status": "skipped",
        "policy_decision": "skipped",
        "exit_code": 0,
        "exit_code_reason": report["exit_code_reason"],
        "timestamp": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_public_json(public_dir(output_dir) / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
        redaction_level=config.security.redaction_level,
    )


def _run_context(
    *,
    config,
    context: ModelContext,
    output_dir: Path,
) -> tuple[dict[str, Any], int]:
    client, branch_id = _client_and_branch_for_context(context, config.omni.timeout)
    all_issues: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    exit_code = 0

    if config.content_validation.enabled:
        content_report, content_exit = run_content_validation(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            user_id=config.omni.user_id,
            include_personal_folders=config.omni.include_personal_folders,
            labels=config.content_validation.labels,
            history_in=output_dir / "history.json",
            history_out=output_dir / "history.json",
            report_out=output_dir / "content-report.json",
            fail_on_new_only=config.content_validation.fail_on_new_only,
            max_samples=config.security.max_report_samples,
            redact_document_names=config.security.redact_document_names,
            allow_raw_response_output=config.security.allow_raw_response_output,
        )
        reports.append(content_report)
        all_issues.extend(content_report.get("issues", []))
        exit_code = max(exit_code, content_exit)

    if config.model_validation.enabled:
        model_report, model_exit = run_model_validation(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            fail_on_warnings=config.model_validation.fail_on_warnings,
        )
        reports.append(model_report)
        all_issues.extend(model_report.get("issues", []))
        exit_code = max(exit_code, model_exit)

    diff_report = None
    head_graph = None
    if config.semantic_lint.enabled or config.contracts.enabled:
        base_yaml_dir = output_dir / "yaml-base"
        head_yaml_dir = output_dir / "yaml-head"
        pull_yaml(
            client=client,
            model_id=context.model_id,
            branch_id=None,
            output_dir=base_yaml_dir,
        )
        pull_yaml(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            output_dir=head_yaml_dir,
        )
        base_graph = build_graph(load_yaml_files(base_yaml_dir))
        head_graph = build_graph(load_yaml_files(head_yaml_dir))
        diff_report = diff_graphs(base_graph, head_graph)
        write_json_report(output_dir / "semantic-diff.json", diff_report)

    if config.semantic_lint.enabled and head_graph is not None:
        lint_issues = lint_graph(
            head_graph,
            configured_rules=config.semantic_lint.rules,
            include_personal_folders=config.omni.include_personal_folders,
            diff_result=diff_report,
        )
        lint_report = {
            "tool": "omniflow",
            "validator": "semantic_lint",
            "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "model_id": context.model_id,
            "branch_id": branch_id,
            "issues": lint_issues,
            "summary": {
                "total_issues": len(lint_issues),
                "errors": sum(1 for issue in lint_issues if issue.get("severity") == "error"),
                "warnings": sum(1 for issue in lint_issues if issue.get("severity") == "warning"),
            },
        }
        reports.append(lint_report)
        all_issues.extend(lint_issues)
        exit_code = max(exit_code, 1 if has_error(lint_issues) else 0)

    if config.contracts.enabled and diff_report is not None:
        dependencies = generate_downstream_dependencies(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            diff_result=diff_report,
            user_id=config.omni.user_id,
            include_personal_folders=config.omni.include_personal_folders,
        )
        write_json_report(output_dir / "dependencies.json", dependencies)
        contract_report, contract_exit = evaluate_contracts(
            diff_result=diff_report,
            dependencies=dependencies,
            settings=config.contracts,
            model_id=context.model_id,
        )
        write_json_report(output_dir / "contract-impact.json", contract_report)
        reports.append(contract_report)
        all_issues.extend(contract_report.get("issues", []))
        exit_code = max(exit_code, contract_exit)

    if config.dbt_exposures.enabled:
        exposure_report, exposure_exit = run_dbt_exposure_enrichment(
            client=client,
            model_id=context.model_id,
            settings=config.dbt_exposures,
        )
        write_json_report(output_dir / "dbt-exposures.json", exposure_report)
        reports.append(exposure_report)
        all_issues.extend(exposure_report.get("issues", []))
        exit_code = max(exit_code, exposure_exit)

    summary = _summarize(all_issues)
    report = _base_report(config, context, branch_id, exit_code, all_issues, summary)
    report["check_reports"] = reports
    write_json_report(output_dir / "report.json", report)
    return report, exit_code


def _write_context_failure_artifacts(
    *,
    config,
    context: ModelContext,
    output_dir: Path,
    exc: OmniCIError,
) -> tuple[dict[str, Any], int]:
    issue = {
        "severity": "error",
        "validator": "context",
        "message": redact(str(exc)),
    }
    summary = _summarize([issue])
    report = _base_report(config, context, context.branch_id, exc.exit_code, [issue], summary)
    report["check_reports"] = []
    report["exit_code_reason"] = _exit_code_reason(exc.exit_code)
    write_json_report(output_dir / "report.json", report)
    return report, exc.exit_code


def cmd_content_validate(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    client, branch_id = _client_and_branch(config)
    output_dir = Path(config.reporting.output_dir)
    labels = _labels_from_args(args) or config.content_validation.labels
    fail_on_new_only = config.content_validation.fail_on_new_only if args.fail_on_new_only is None else args.fail_on_new_only
    report, exit_code = run_content_validation(
        client=client,
        model_id=_required(config.omni.model_id, "omni.model_id"),
        branch_id=branch_id,
        user_id=config.omni.user_id,
        include_personal_folders=config.omni.include_personal_folders,
        labels=labels,
        history_in=args.history_in or output_dir / "history.json",
        history_out=args.history_out or output_dir / "history.json",
        report_out=args.report_out or output_dir / "report.json",
        fail_on_new_only=fail_on_new_only,
        max_samples=config.security.max_report_samples,
        redact_document_names=config.security.redact_document_names,
        allow_raw_response_output=config.security.allow_raw_response_output,
    )
    print(
        "Content validator results: "
        f"total={report['total_issues']} new={report['new_issues']} "
        f"existing={report['existing_issues']} resolved={report['resolved_issues']}"
    )
    return exit_code


def cmd_model_validate(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    client, branch_id = _client_and_branch(config)
    fail_on_warnings = config.model_validation.fail_on_warnings if args.fail_on_warnings is None else args.fail_on_warnings
    report, exit_code = run_model_validation(
        client=client,
        model_id=_required(config.omni.model_id, "omni.model_id"),
        branch_id=branch_id,
        fail_on_warnings=fail_on_warnings,
    )
    write_json_report(Path(config.reporting.output_dir) / "model-report.json", report)
    print(f"Model validation: errors={report['summary']['errors']} warnings={report['summary']['warnings']}")
    return exit_code


def cmd_yaml_pull(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    client, branch_id = _client_and_branch(config)
    out = args.out or str(Path(config.reporting.output_dir) / "yaml")
    manifest = pull_yaml(
        client=client,
        model_id=_required(config.omni.model_id, "omni.model_id"),
        branch_id=branch_id,
        output_dir=out,
        mode=args.mode,
        fully_resolved=args.fully_resolved,
    )
    print(f"Pulled {len(manifest['files'])} YAML file(s) to {out}")
    return 0


def cmd_exposures_pull(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    client, _ = _client_and_branch(config)
    report, exit_code = run_dbt_exposure_enrichment(
        client=client,
        model_id=_required(config.omni.model_id, "omni.model_id"),
        settings=config.dbt_exposures,
    )
    output_path = Path(args.out or Path(config.reporting.output_dir) / "dbt-exposures.json")
    write_json_report(output_path, report)
    print(f"Pulled {report['summary']['total_exposures']} dbt exposure record(s) to {output_path}")
    return exit_code


def cmd_diff(args: argparse.Namespace) -> int:
    base_graph = build_graph(load_yaml_files(args.base))
    head_graph = build_graph(load_yaml_files(args.head))
    report = diff_graphs(base_graph, head_graph)
    if args.report_out:
        write_json_report(args.report_out, report)
    else:
        print(redact(report))
    return 1 if report["risk_level"] in {"breaking", "security_sensitive"} else 0


def cmd_report(args: argparse.Namespace) -> int:
    import json

    report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    write_reports(report, output_dir=args.output_dir, formats=args.format)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    missing = []
    api_key = require_api_key()
    if not api_key:
        missing.append("OMNI_API_KEY")
    contexts = []
    if args.auto:
        contexts = discover_contexts(
            auto=True,
            base_url=config.omni.base_url,
            model_id=config.omni.model_id,
            model_path=getattr(args, "model_path", None),
            branch_name=config.omni.branch_name,
            branch_id=config.omni.branch_id,
        )
        if not contexts:
            missing.append(".omni/flow.json model context")
    else:
        if not config.omni.base_url:
            missing.append("--base-url or OMNI_BASE_URL")
        if not config.omni.model_id:
            missing.append("--model-id or OMNI_MODEL_ID")
    if missing:
        raise ConfigError(f"Missing required values: {', '.join(missing)}")
    issues = []
    for context in contexts:
        client = OmniClient(base_url=context.base_url, api_key=api_key, timeout=config.omni.timeout)
        git_config = client.get_git_configuration(context.model_id)
        issues.extend(_git_configuration_issues(context, git_config))
    if issues:
        raise ConfigError("Omni Git configuration mismatch: " + "; ".join(issues))
    print(f"omniflow doctor passed: {len(contexts) or 1} model context(s) ready")
    return 0


def _client_and_branch(config):
    context = ModelContext(
        base_url=_required(config.omni.base_url, "base_url"),
        model_id=_required(config.omni.model_id, "model_id"),
        model_path="",
        branch_name=config.omni.branch_name,
        branch_id=config.omni.branch_id,
    )
    return _client_and_branch_for_context(context, config.omni.timeout)


def _client_and_branch_for_context(context: ModelContext, timeout: int):
    client = OmniClient(
        base_url=context.base_url,
        api_key=require_api_key(),
        timeout=timeout,
    )
    branch_id = context.branch_id or client.resolve_branch_id(context.model_id, context.branch_name)
    if context.branch_name and not branch_id:
        raise ConfigError(
            f"Could not resolve Omni branch '{context.branch_name}' for model {context.model_id}. "
            "Verify the Omni PR branch exists and the API key can list model branches."
        )
    return client, branch_id


def _override_config(config, args: argparse.Namespace):
    for attr in ("base_url", "model_id", "branch_id", "branch_name", "user_id"):
        value = getattr(args, attr, None)
        if value:
            setattr(config.omni, attr, value)
    if getattr(args, "include_personal_folders", None) is not None:
        config.omni.include_personal_folders = args.include_personal_folders
    return config


def _labels_from_args(args: argparse.Namespace) -> list[str]:
    values = [*(getattr(args, "labels", []) or []), *(getattr(args, "label", []) or [])]
    labels: list[str] = []
    for value in values:
        for name in value.split(","):
            stripped = name.strip()
            if stripped and stripped not in labels:
                labels.append(stripped)
    return labels


def _base_report(config, context: ModelContext, branch_id: str | None, exit_code: int, issues: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "omni_base_url": context.base_url,
        "model_id": context.model_id,
        "model_path": context.model_path,
        "branch_id": branch_id,
        "branch_name": context.branch_name,
        "config_hash": config.hash,
        "summary": summary,
        "issues": issues,
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
        "exit_code_reason": "validation failed" if exit_code else "success",
    }


def _aggregate_report(config, contexts, exit_code, issues, summary, reports):
    return {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "models": [_context_dict(context) for context in contexts],
        "config_hash": config.hash,
        "summary": summary,
        "issues": issues,
        "model_reports": reports,
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
        "exit_code_reason": "validation failed" if exit_code else "success",
    }


def _summarize(issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_issues": len(issues),
        "errors": sum(1 for issue in issues if issue.get("severity") == "error"),
        "warnings": sum(1 for issue in issues if issue.get("severity") in {"warning", "warn"}),
        "new_issues": sum(1 for issue in issues if issue.get("state") == "new"),
        "existing_issues": sum(1 for issue in issues if issue.get("state") == "existing"),
        "resolved_issues": sum(1 for issue in issues if issue.get("state") == "resolved"),
        "risk_level": "breaking" if any(issue.get("risk") == "breaking" for issue in issues) else "info",
    }


def _exit_code_reason(exit_code: int) -> str:
    return {
        ExitCodes.SUCCESS: "success",
        ExitCodes.VALIDATION_FAILED: "validation failed",
        ExitCodes.CONFIGURATION_ERROR: "configuration error",
        ExitCodes.AUTHORIZATION_ERROR: "authentication or authorization error",
        ExitCodes.OMNI_API_ERROR: "Omni API error",
        ExitCodes.SECURITY_POLICY_VIOLATION: "security policy violation",
        ExitCodes.INTERNAL_ERROR: "internal tool error",
    }.get(exit_code, "unknown error")


def _required(value: Any, name: str) -> Any:
    if not value:
        raise ConfigError(f"Missing required value: {name}")
    return value


def _safe_context_dir(context: ModelContext) -> str:
    return context.model_id.replace("/", "_")


def _context_dict(context: ModelContext) -> dict[str, Any]:
    return {
        "base_url": context.base_url,
        "model_id": context.model_id,
        "model_path": context.model_path,
        "branch_name": context.branch_name,
        "branch_id": context.branch_id,
        "base_branch": context.base_branch,
        "git_provider": context.git_provider,
        "web_url": context.web_url,
    }


def _git_configuration_issues(context: ModelContext, git_config: dict[str, Any]) -> list[str]:
    checks = [
        ("model_path", context.model_path, git_config.get("modelPath")),
        ("base_branch", context.base_branch, git_config.get("baseBranch")),
        ("git_provider", context.git_provider, git_config.get("gitServiceProvider")),
        ("web_url", context.web_url, git_config.get("webUrl")),
    ]
    issues = []
    for label, expected, actual in checks:
        if not expected or actual is None:
            continue
        if _normalize_git_config_value(expected) != _normalize_git_config_value(actual):
            issues.append(f"{label} expected {expected!r} but Omni reports {actual!r}")
    return issues


def _normalize_git_config_value(value: Any) -> str:
    return str(value).strip().strip("/").lower()




if __name__ == "__main__":
    raise SystemExit(main())
