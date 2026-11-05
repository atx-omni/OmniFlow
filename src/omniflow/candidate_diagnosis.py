"""Read-only, local-only troubleshooting of an already reviewed GitHub PR.

Unlike a CI run, this prints bounded customer paths to the operator. It does
not create a report, run validators, modify branches, or establish readiness.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .candidate import (
    MAX_TREE_ENTRIES,
    CandidateVerificationError,
    _bounded_git,
    _GitUnavailable,
    _local_tree,
    _safe_path,
    _verify_snapshot,
)
from .candidate_difference import shareable_difference_fields
from .config import DEFAULT_CONFIG_PATH, OmniFlowConfig, _to_config, parse_policy_text, require_api_key
from .diagnostics import CANDIDATE_REASONS, safe_exception_message
from .discovery import FLOW_PATH, _model_from_payload, parse_flow_metadata_text
from .exceptions import ConfigError, ExitCodes, OmniFlowError
from .github.revalidation import GitHubRepository
from .omni_client import OmniClient
from .repository_locations import _path as displayable_path
from .revision_data import FULL_SHA, REPOSITORY
from .security import redact, validate_base_url, validate_branch_name
from .trust import MAX_TRUSTED_FILE_BYTES

MAX_PREVIEW_CHARS = 160
_EOL = re.compile(r"\r\n|\r|\n")
_CREDENTIAL_LABEL = re.compile(
    r"api[ _-]?key|token|secret|password|authorization|bearer|credential|private[ _-]?key|access[ _-]?key",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE = re.compile(
    r"-----BEGIN |gh[pousr]_|github_pat_|sk[-_]|xox[baprs]-|(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]+\.|[A-Za-z0-9_+/=-]{32,}|(?<!\d)\d(?:[ _-]?\d){8,}(?!\d)", re.IGNORECASE,
)


class DiagnosisSetupError(ConfigError):
    """Only fixed, customer-independent instructions belong in these messages."""


@dataclass(frozen=True)
class PRSnapshot:
    base_sha: str
    head_sha: str
    base_ref: str
    head_ref: str


def _preview_line(text: str, number: int, offset: int, secrets: tuple[str, ...]) -> str:
    """Extract one local display window. Screen the entire line before truncation."""
    start = 0
    for i, end in enumerate(_EOL.finditer(text), 1):
        if i == number:
            stop = end.start()
            break
        start = end.end()
    else:
        stop = len(text)
        if stop == start:
            return "[end of file]"
    if stop - start > 4096:
        return "[line too long for a safe preview]"
    line = text[start:stop]
    if (redact(line) != line or _CREDENTIAL_LABEL.search(line) or _CREDENTIAL_VALUE.search(line)
            or any(secret in line for secret in secrets)):
        return "[withheld: sensitive pattern]"
    if len(line) <= MAX_PREVIEW_CHARS:
        return line
    left = max(0, min(len(line) - MAX_PREVIEW_CHARS + 2, offset - start - MAX_PREVIEW_CHARS // 2))
    right = left + MAX_PREVIEW_CHARS - 2
    return ("…" if left else "") + line[left:right] + ("…" if right < len(line) else "")


def _terminal_preview(git: str, omni: str, details: dict) -> dict:
    if details["difference_class"] in {"line_endings_only", "terminal_newline_only"}:
        return {}  # Direction/style is the actionable difference; no excerpt needed.
    number = details["first_differing_line"]
    offset = next((i for i, (a, b) in enumerate(zip(git, omni, strict=False)) if a != b),
                  min(len(git), len(omni)))
    secrets = tuple(piece for name, value in os.environ.items() if value and _CREDENTIAL_LABEL.search(name)
                    for piece in value.splitlines() if piece)
    return {"git": _preview_line(git, number, offset, secrets), "omni": _preview_line(omni, number, offset, secrets)}


def _interactive_terminal() -> bool:
    return sys.stdout.isatty() and sys.stderr.isatty()


def _check_local(args) -> None:
    if any(os.environ.get(k, "").lower() not in {"", "0", "false", "no"} for k in (
        "CI", "GITHUB_ACTIONS", "GITHUB_EVENT_NAME", "GITLAB_CI", "BUILDKITE", "TF_BUILD", "JENKINS_URL",
    )):
        raise DiagnosisSetupError("Refused: run this private path diagnostic on an approved local workstation, not in CI.")
    if args.verbose:
        raise DiagnosisSetupError("Do not enable --verbose for private candidate diagnostics.")
    if args.show_diff and not _interactive_terminal():
        raise DiagnosisSetupError("--show-diff requires an approved interactive terminal; do not pipe, capture or upload source.")
    if (not REPOSITORY.fullmatch(args.repository) or not 1 <= args.pr <= 2**31 - 1
            or not FULL_SHA.fullmatch(args.head_sha) or (args.base_sha and not FULL_SHA.fullmatch(args.base_sha))):
        raise DiagnosisSetupError("Use a GitHub OWNER/REPO, positive PR number and full Git commit SHAs, not refs.")
    if args.scope == "base" and not args.base_sha:
        raise DiagnosisSetupError("For --scope base, also provide --base-sha from the failing run.")
    if os.getenv("GITHUB_API_URL", "https://api.github.com") != "https://api.github.com":
        raise DiagnosisSetupError("Only the standard GitHub.com API origin is supported. Do not redirect GitHub credentials.")


def _read_pr(api: GitHubRepository, args, *, repo_id: int, default_branch: str) -> PRSnapshot:
    pr = api.request("GET", f"/pulls/{args.pr}")
    if (not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] != args.pr
            or pr.get("state") != "open"):
        raise DiagnosisSetupError("Select the open PR for the failing run. Closed or historical PR snapshots are unsupported.")
    sides = []
    for key in ("base", "head"):
        side = pr.get(key)
        repo = side.get("repo") if isinstance(side, dict) else None
        if (not isinstance(repo, dict) or repo.get("full_name") != args.repository
                or type(repo.get("id")) is not int or repo["id"] != repo_id):
            raise DiagnosisSetupError("Only a same-repository PR is supported; never use an Omni credential with a fork.")
        sha, ref = side.get("sha"), side.get("ref")
        if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha) or not isinstance(ref, str):
            raise DiagnosisSetupError("GitHub did not provide exact PR revisions and branches. Refresh the PR metadata.")
        sides.append((sha.lower(), validate_branch_name(ref)))
    (base, base_ref), (head, head_ref) = sides
    if base_ref != default_branch:
        raise DiagnosisSetupError("Only PRs targeting the repository default branch are supported. Do not retarget a release PR.")
    if head != args.head_sha.lower() or (args.base_sha and base != args.base_sha.lower()):
        raise DiagnosisSetupError("The PR moved since the supplied revision. Run fresh CI and use that run's PR revisions.")
    return PRSnapshot(base, head, base_ref, head_ref)


def _checkout_inputs(sha: str, policy_path: str) -> tuple[dict, OmniFlowConfig]:
    try:
        policy = Path(_safe_path(policy_path))
    except CandidateVerificationError:
        raise DiagnosisSetupError("--config must be a canonical repository-relative path to the trusted base policy.") from None
    try:
        if (_bounded_git(["rev-parse", "--show-prefix"], root=Path("."), limit=1024, local_only=True).strip()
                or _bounded_git(["rev-parse", "HEAD"], root=Path("."), limit=100, local_only=True).decode().strip() != sha):
            raise DiagnosisSetupError("Run from the customer repository root in a trusted checkout of the exact PR base SHA.")
        tree = _local_tree(sha, root=Path("."), local_only=True)
        if len(tree) > MAX_TREE_ENTRIES:
            raise DiagnosisSetupError("Trusted base exceeds the diagnostic repository inventory limit.")
        entries = {e["path"]: e for e in tree}
        def base_text(path: str) -> str:
            entry = entries.get(path, {})
            if (entry.get("mode") not in {"100644", "100755"} or entry.get("type") != "blob"
                    or not FULL_SHA.fullmatch(str(entry.get("sha", "")))
                    or type(entry.get("size")) is not int or not 0 < entry["size"] <= MAX_TRUSTED_FILE_BYTES):
                raise DiagnosisSetupError("The trusted base must contain regular, bounded .omni/flow.json and the selected policy.")
            raw = _bounded_git(["cat-file", "blob", entry["sha"]], root=Path("."), limit=MAX_TRUSTED_FILE_BYTES,
                               local_only=True)
            algorithm = hashlib.sha1 if len(entry["sha"]) == 40 else hashlib.sha256  # nosec B303,B324
            blob = algorithm(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()  # nosec B303,B324
            if len(raw) != entry["size"] or blob != entry["sha"].lower():
                raise DiagnosisSetupError("The local metadata blob does not match the trusted base. Use a verified fresh clone.")
            return raw.decode()
        flow = parse_flow_metadata_text(base_text(str(FLOW_PATH)))
        config = _to_config(parse_policy_text(base_text(str(policy)), source=policy), policy)
    except (_GitUnavailable, UnicodeError):
        raise DiagnosisSetupError("Fetch the exact base and head Git objects; run from the trusted base checkout root.") from None
    return flow, config


def _path_label(value: str) -> str:
    blocked = [os.getenv(k) for k in ("OMNI_API_KEY", "OMNIFLOW_GITHUB_TOKEN")]
    return value if displayable_path(value) and not any(k and k in value for k in blocked) else "[withheld]"


def _diagnose(args) -> tuple[int, dict]:
    _check_local(args)
    expected_host = os.getenv("OMNI_BASE_URL")
    if not expected_host or not validate_base_url(expected_host).startswith("https://"):
        raise DiagnosisSetupError("Set OMNI_BASE_URL to your independently approved HTTPS Omni tenant before running.")
    api = GitHubRepository(args.repository, os.getenv("OMNIFLOW_GITHUB_TOKEN", ""))
    metadata = api.request("GET", "")
    if (not isinstance(metadata, dict) or metadata.get("full_name") != args.repository
            or type(metadata.get("id")) is not int or not isinstance(metadata.get("default_branch"), str)):
        raise DiagnosisSetupError("Cannot verify the GitHub repository. Check OWNER/REPO and the read-only GitHub credential.")
    binding = {"repo_id": metadata["id"], "default_branch": metadata["default_branch"]}
    pr = _read_pr(api, args, **binding)
    flow, config = _checkout_inputs(pr.base_sha, args.config or DEFAULT_CONFIG_PATH)
    head = args.scope == "branch"
    contexts = [_model_from_payload(m, branch_name=pr.head_ref if head else None, version=flow["version"])
                for m in flow["models"]]
    eligible = [m for m in contexts if (not m.base_branch or m.base_branch == pr.base_ref)
                and (not args.model_path or args.model_path == m.model_path)]
    if len(eligible) != 1:
        raise DiagnosisSetupError("Select one exact registered --model-path from the trusted base .omni/flow.json.")
    context = eligible[0]
    if context.web_url and context.web_url.rstrip("/").removesuffix(".git") != f"https://github.com/{args.repository}":
        raise DiagnosisSetupError("The registered Git repository differs from the selected repository. Check trusted metadata.")
    if context.base_url != validate_base_url(expected_host):
        raise DiagnosisSetupError("OMNI_BASE_URL differs from trusted registration. Verify the intended tenant; no Omni credential was sent.")
    if any((config.omni.model_id, config.omni.branch_id, config.omni.branch_name)):
        raise DiagnosisSetupError("Explicit model/branch overrides are unsupported. Use the registered automatic validation workflow.")
    side, sha = ("head", pr.head_sha) if head else ("base", pr.base_sha)
    if context.environment:
        if os.getenv("OMNIFLOW_ENVIRONMENT") != context.environment:
            raise DiagnosisSetupError("Set OMNIFLOW_ENVIRONMENT to the independently approved target and use its dedicated "
                                      "validation credential. The trusted registration must match; no Omni credential was sent.")
        # Use the production v2 environment, live Git settings, follower and
        # exact candidate-branch checks; never select a target from a PR marker.
        from .cli import _client_and_branch_for_context

        client, _ = _client_and_branch_for_context(context, config.omni.timeout)
    else:
        client = OmniClient(base_url=context.base_url, timeout=config.omni.timeout, api_key=require_api_key())
        if head:
            context.branch_id = client.resolve_branch_id(context.model_id, pr.head_ref)
            if not context.branch_id:
                raise DiagnosisSetupError("No Omni branch matches this PR head name. Verify the saved Omni branch and Git integration.")
    output = {"diagnostic": "local_candidate_comparison", "scope": args.scope, "git_revision": sha,
              "model_path": _path_label(context.model_path) if context.model_path != "." else ".",
              "not_a_validation_run": True}
    preview: dict[str, str] = {}
    def capture_preview(git: str, omni: str, details: dict) -> None:
        preview.update(_terminal_preview(git, omni, details))

    try:
        proof = _verify_snapshot(client, context, revision_side=side, non_omni_rules=config.files.non_omni,
                                 local_diagnosis_revision=(sha, args.repository),
                                 local_preview=capture_preview if args.show_diff else None)
    except CandidateVerificationError as exc:
        reason = exc.candidate_reason if exc.candidate_reason in CANDIDATE_REASONS else "verification_unavailable"
        code = exc.exit_code
        output.update(status="different" if reason == "content_mismatch" else "incomplete",
                      reason=reason, **exc.candidate_counts)
        samples = []
        for observation in exc.candidate_samples:
            item = {key: observation[key] for key in ("pairing", "comparison") if key in observation}
            for key in ("git_path", "omni_path"):
                value = observation.get(key)
                if isinstance(value, str):
                    if key == "git_path" and context.model_path != ".":
                        value = f"{context.model_path}/{value}"
                    item[key] = _path_label(value)
            if "first_differing_line" in observation:
                item.update(first_differing_line=observation["first_differing_line"],
                            **shareable_difference_fields(observation))
            if item:
                samples.append(item)
        output["samples"] = samples
        if reason == "content_mismatch":
            output["files_not_compared"] = exc.candidate_counts["git_file_count"] - exc.candidate_counts["compared_file_count"]
            output["next_action"] = ("Stopped at first difference. Compare the named Git file at git_revision with the saved "
                                     "Omni file on this scope. Review and synchronize the intended version; run fresh CI.")
            if any("[withheld]" in sample.values() for sample in samples):
                output["next_action"] = ("Stopped at the first difference; a path label could not safely be displayed. "
                                         "Use the recorded revision, scope and line in your approved private comparison "
                                         "environment. Do not share raw paths or contents.")
        elif reason == "git_snapshot_unavailable":
            output["next_action"] = "Fetch the exact PR Git objects into the trusted checkout without checking out PR code."
        else:
            output["next_action"] = "Use the recorded reason in the candidate troubleshooting guide. Do not bypass the gate."
    else:
        code = ExitCodes.SUCCESS
        output.update(status="matched", compared_file_count=proof["file_count"],
                      next_action="Observed snapshots match now. Run fresh CI; this does not validate the PR or reconstruct the old run.")
    if _read_pr(api, args, **binding) != pr:
        raise DiagnosisSetupError("The PR changed during diagnosis. Discard these observations and start a fresh run.")
    if preview and all("[withheld]" not in sample.values() for sample in output.get("samples", [])):
        output["source_preview"] = preview
    return code, output


def cmd_candidate_diagnose(args) -> int:
    try:
        code, result = _diagnose(args)
        if args.show_diff and not _interactive_terminal():
            raise DiagnosisSetupError("Source preview canceled: output is not an interactive terminal.")
    except DiagnosisSetupError as exc:
        print(str(exc), file=sys.stderr)  # Literal setup instructions, never remote payloads.
        return exc.exit_code
    except OmniFlowError as exc:
        print(safe_exception_message(exc), file=sys.stderr)
        return exc.exit_code
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return code
