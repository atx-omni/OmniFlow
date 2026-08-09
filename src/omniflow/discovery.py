from __future__ import annotations

import json
import os
import re

# Git is invoked without a shell and with bounded arguments.
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .exceptions import ConfigError, SecurityPolicyError
from .git import current_branch, git_executable, github_event_payload, is_pull_request_event, pr_number
from .security import reject_secret_keys, validate_base_url, validate_branch_name, validate_path_segment
from .trust import read_trusted_repo_text

FLOW_PATH = Path(".omni/flow.json")
PR_MARKER_RE = re.compile(r"<!--\s*omniflow-context\s+({.*?})\s*-->", re.DOTALL)
PR_MARKER_KEYS = {"model_id", "model_path", "branch_name", "base_url"}


@dataclass
class ModelContext:
    base_url: str
    model_id: str
    model_path: str
    branch_name: str | None = None
    branch_id: str | None = None
    base_branch: str | None = None
    git_provider: str | None = None
    web_url: str | None = None


def discover_contexts(
    *,
    auto: bool,
    base_url: str | None = None,
    model_id: str | None = None,
    model_path: str | None = None,
    branch_name: str | None = None,
    branch_id: str | None = None,
    flow_path: str | Path = FLOW_PATH,
    allow_skip: bool = False,
) -> list[ModelContext]:
    branch = branch_name or discover_branch_name()
    if base_url and model_id:
        context = _model_from_payload(
            {"base_url": base_url, "model_id": model_id, "model_path": model_path or ""},
            branch_name=branch,
            require_model_path=False,
        )
        context.branch_id = branch_id
        return [context]
    if not auto:
        raise ConfigError("Missing model identity. Use --auto or provide --base-url and --model-id.")

    marker = load_pr_marker()
    if marker.get("base_url"):
        raise SecurityPolicyError(
            "OmniFlow PR markers cannot provide base_url by default. "
            "Use .omni/flow.json, OMNI_BASE_URL, or --base-url from a trusted workflow source."
        )

    changed_files = get_changed_files()
    flow = load_flow_metadata(flow_path, missing_ok=allow_skip and not marker)
    if flow is None:
        if any(_is_probable_omni_file(path) for path in changed_files):
            raise ConfigError(
                "Omni semantic files changed, but trusted .omni/flow.json metadata is missing. "
                "Commit model identity metadata to the base branch before merging Omni changes."
            )
        return []
    contexts = select_model_contexts(
        flow,
        changed_files=changed_files,
        marker=marker,
        branch_name=branch,
        allow_skip=allow_skip,
    )
    _validate_pull_request_base_branch(contexts)
    if base_url:
        trusted_base_url = validate_base_url(base_url)
        for context in contexts:
            context.base_url = trusted_base_url
    if branch_id:
        for context in contexts:
            context.branch_id = branch_id
    return contexts


def discover_branch_name() -> str | None:
    return current_branch()


def load_flow_metadata(path: str | Path = FLOW_PATH, *, missing_ok: bool = False) -> dict[str, Any] | None:
    candidate = Path(path)
    text = read_trusted_repo_text(candidate)
    if text is None and missing_ok:
        return None
    if text is None:
        raise ConfigError(f"Missing OmniFlow metadata file: {candidate}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Could not parse metadata file '{candidate}': {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"Metadata file {candidate} must contain a JSON object")
    reject_secret_keys(payload, source=str(candidate))
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise ConfigError(f"Metadata file {candidate} must include a non-empty models list")
    if payload.get("version") != 1:
        raise ConfigError(f"Metadata file {candidate} must use version 1")
    if any(not isinstance(item, dict) for item in models):
        raise ConfigError(f"Metadata file {candidate} models must all be JSON objects")
    contexts = [_model_from_payload(item, branch_name=None) for item in models]
    model_ids = [context.model_id for context in contexts]
    model_paths = [context.model_path for context in contexts]
    if len(model_ids) != len(set(model_ids)):
        raise ConfigError(f"Metadata file {candidate} contains duplicate model_id values")
    if len(model_paths) != len(set(model_paths)):
        raise ConfigError(f"Metadata file {candidate} contains duplicate model_path values")
    return payload


def load_pr_marker() -> dict[str, Any]:
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    body = (event.get("pull_request") or {}).get("body") or ""
    if not isinstance(body, str):
        return {}
    match = PR_MARKER_RE.search(body)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"OmniFlow PR marker contains invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("OmniFlow PR marker must contain a JSON object")
    reject_secret_keys(payload, source="omniflow-context PR marker")
    unknown = sorted(set(payload) - PR_MARKER_KEYS)
    if unknown:
        raise ConfigError(f"OmniFlow PR marker contains unsupported key(s): {', '.join(unknown)}")
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"OmniFlow PR marker value for {key} must be a non-empty string")
        payload[key] = value.strip()
    if payload.get("branch_name"):
        validate_branch_name(payload["branch_name"])
    return payload


def get_changed_files(base_branch: str | None = None) -> list[str]:
    explicit = os.getenv("OMNIFLOW_CHANGED_FILES")
    if explicit:
        return [item.strip() for item in explicit.splitlines() if item.strip()]

    if os.getenv("GITHUB_EVENT_NAME") == "pull_request_target":
        return _github_pull_request_files()

    candidates = []
    if os.getenv("GITHUB_BASE_REF"):
        candidates.append(f"origin/{os.getenv('GITHUB_BASE_REF')}...HEAD")
    if base_branch:
        candidates.append(f"origin/{base_branch}...HEAD")
    candidates.extend(["HEAD~1...HEAD", "--cached"])

    for candidate in candidates:
        try:
            cmd = [git_executable(), "diff", "--name-only", "--no-renames", candidate]
            # Arguments are passed directly to Git, never through a shell.
            result = subprocess.run(  # nosec B603
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if files:
            return files
    return []


def select_model_contexts(
    flow: dict[str, Any],
    *,
    changed_files: list[str],
    marker: dict[str, Any] | None = None,
    branch_name: str | None = None,
    allow_skip: bool = False,
) -> list[ModelContext]:
    models = [_model_from_payload(item, branch_name=branch_name) for item in flow["models"]]
    marker = marker or {}
    if marker.get("model_id"):
        matching = [context for context in models if context.model_id == marker["model_id"]]
        if not matching:
            raise ConfigError("OmniFlow PR marker references a model_id not present in .omni/flow.json")
        for context in matching:
            marker_path = str(marker.get("model_path") or "").strip().strip("/")
            if marker_path and marker_path != context.model_path:
                raise SecurityPolicyError(
                    "OmniFlow PR marker model_path does not match trusted .omni/flow.json metadata"
                )
            marker_branch = marker.get("branch_name")
            if marker_branch and branch_name and marker_branch != branch_name:
                raise SecurityPolicyError(
                    "OmniFlow PR marker branch_name does not match the GitHub pull request head branch"
                )
            context.branch_name = branch_name or marker_branch
            _clear_base_branch_context(context)
        return matching

    matched = [
        context
        for context in models
        if context.model_path and any(_is_under_model_path(path, context.model_path) for path in changed_files)
    ]
    if matched:
        for context in matched:
            _clear_base_branch_context(context)
        return matched
    if allow_skip and changed_files:
        unmatched_omni_files = [path for path in changed_files if _is_probable_omni_file(path)]
        if unmatched_omni_files:
            sample = ", ".join(unmatched_omni_files[:3])
            raise ConfigError(
                "Omni semantic files changed outside every registered model_path: "
                f"{sample}. Update trusted .omni/flow.json metadata or add a valid PR marker."
            )
        return []
    if allow_skip and not changed_files:
        return []
    if len(models) == 1:
        _clear_base_branch_context(models[0])
        return models
    raise ConfigError("Could not select an Omni model from changed files. Add an omniflow-context PR marker.")


def _model_from_payload(
    payload: dict[str, Any], *, branch_name: str | None, require_model_path: bool = True
) -> ModelContext:
    keys = ("base_url", "model_id", "model_path") if require_model_path else ("base_url", "model_id")
    for key in keys:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ConfigError(f"OmniFlow model context must include {key}")
    base_url = validate_base_url(payload["base_url"].strip())
    model_id = validate_path_segment(payload["model_id"].strip(), name="model_id")
    normalized_model_path = str(payload.get("model_path") or "").strip().strip("/")
    if normalized_model_path and (
        Path(normalized_model_path).is_absolute() or ".." in Path(normalized_model_path).parts
    ):
        raise ConfigError("OmniFlow model_path must stay inside the repository")
    base_branch = payload.get("base_branch")
    if base_branch is not None:
        if not isinstance(base_branch, str) or not base_branch.strip():
            raise ConfigError("OmniFlow base_branch must be a non-empty string when provided")
        base_branch = validate_branch_name(base_branch.strip())
    return ModelContext(
        base_url=base_url,
        model_id=model_id,
        model_path=normalized_model_path,
        branch_name=branch_name,
        base_branch=base_branch,
        git_provider=payload.get("git_provider"),
        web_url=payload.get("web_url"),
    )


def _validate_pull_request_base_branch(contexts: list[ModelContext]) -> None:
    github_base_branch = os.getenv("GITHUB_BASE_REF")
    if not is_pull_request_event() or not github_base_branch:
        return
    github_base_branch = validate_branch_name(github_base_branch)
    for context in contexts:
        if context.base_branch and context.base_branch != github_base_branch:
            raise ConfigError(
                f"Pull request targets '{github_base_branch}', but model {context.model_id} is configured "
                f"for base branch '{context.base_branch}'. Retarget the PR or correct trusted .omni/flow.json."
            )


def _is_under_model_path(path: str, model_path: str) -> bool:
    normalized_path = path.strip().strip("/")
    normalized_model_path = model_path.strip().strip("/")
    if normalized_model_path == ".":
        return _is_probable_omni_file(normalized_path)
    return normalized_path == normalized_model_path or normalized_path.startswith(f"{normalized_model_path}/")


def _clear_base_branch_context(context: ModelContext) -> None:
    if is_pull_request_event():
        return
    if context.base_branch and context.branch_name == context.base_branch:
        context.branch_name = None


def _is_probable_omni_file(path: str) -> bool:
    normalized = path.strip().strip("/").lower()
    candidate = Path(normalized)
    name = candidate.name
    if name in {"model.yaml", "model.yml", "relationships.yaml", "relationships.yml"}:
        return True
    if any(name.endswith(suffix) for suffix in (".view", ".topic", ".relationships")):
        return True
    parts = candidate.parts
    return bool(
        {"views", "topics", "relationships"}.intersection(parts[:-1])
        and candidate.suffix in {".yaml", ".yml"}
    )


def _github_pull_request_files() -> list[str]:
    token = os.getenv("OMNIFLOW_GITHUB_TOKEN")
    event = github_event_payload()
    repository = event.get("repository") if isinstance(event.get("repository"), dict) else {}
    repo_name = repository.get("full_name") or os.getenv("GITHUB_REPOSITORY")
    number = pr_number()
    if not token:
        raise ConfigError("OMNIFLOW_GITHUB_TOKEN is required to route pull_request_target events safely")
    if not isinstance(repo_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_name):
        raise ConfigError("GitHub event payload did not contain a valid repository name")
    if not number or not number.isdigit():
        raise ConfigError("GitHub event payload did not contain a valid pull request number")

    api_url = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    parsed = urlparse(api_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SecurityPolicyError("GITHUB_API_URL must be a trusted HTTPS origin")

    files: list[str] = []
    record_count = 0
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    for page in range(1, 31):
        try:
            response = requests.get(
                f"{api_url}/repos/{repo_name}/pulls/{number}/files",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=30,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ConfigError("Could not retrieve pull request changed files from GitHub") from exc
        if not response.ok:
            raise ConfigError(f"GitHub changed-file lookup failed with HTTP {response.status_code}")
        try:
            records = response.json()
        except ValueError as exc:
            raise ConfigError("GitHub changed-file lookup returned invalid JSON") from exc
        if not isinstance(records, list):
            raise ConfigError("GitHub changed-file lookup returned an unexpected response shape")
        record_count += len(records)
        for record in records:
            if not isinstance(record, dict):
                continue
            for key in ("filename", "previous_filename"):
                value = record.get(key)
                if isinstance(value, str) and value.strip() and value not in files:
                    files.append(value.strip())
        if len(records) < 100:
            break

    pull_request = event.get("pull_request") if isinstance(event.get("pull_request"), dict) else {}
    expected_count = pull_request.get("changed_files")
    if isinstance(expected_count, int) and record_count < expected_count:
        raise ConfigError(
            "GitHub returned an incomplete pull request file list; OmniFlow cannot route the change safely"
        )
    return files
