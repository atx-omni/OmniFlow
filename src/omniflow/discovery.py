from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exceptions import ConfigError, SecurityPolicyError
from .git import current_branch
from .security import reject_secret_keys


FLOW_PATH = Path(".omni/flow.json")
PR_MARKER_RE = re.compile(r"<!--\s*omniflow-context\s+({.*?})\s*-->", re.DOTALL)


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
        return [
            ModelContext(
                base_url=base_url,
                model_id=model_id,
                model_path=model_path or "",
                branch_name=branch,
                branch_id=branch_id,
            )
        ]
    if not auto:
        raise ConfigError("Missing model identity. Use --auto or provide --base-url and --model-id.")

    marker = load_pr_marker()
    if base_url and marker.get("model_id"):
        context = _model_from_payload(
            {
                "base_url": base_url,
                "model_id": marker["model_id"],
                "model_path": marker.get("model_path") or model_path or "",
                "base_branch": marker.get("base_branch"),
                "git_provider": marker.get("git_provider"),
                "web_url": marker.get("web_url"),
            },
            branch_name=marker.get("branch_name") or branch,
            require_model_path=False,
        )
        if branch_id:
            context.branch_id = branch_id
        return [context]
    if marker.get("base_url"):
        raise SecurityPolicyError(
            "OmniFlow PR markers cannot provide base_url by default. "
            "Use .omni/flow.json, OMNI_BASE_URL, or --base-url from a trusted workflow source."
        )

    if allow_skip and not marker and not Path(flow_path).exists():
        return []
    flow = load_flow_metadata(flow_path)
    changed_files = get_changed_files(base_branch=marker.get("base_branch") or None)
    contexts = select_model_contexts(
        flow,
        changed_files=changed_files,
        marker=marker,
        branch_name=branch,
        allow_skip=allow_skip,
    )
    if branch_id:
        for context in contexts:
            context.branch_id = branch_id
    return contexts


def discover_branch_name() -> str | None:
    return os.getenv("GITHUB_HEAD_REF") or os.getenv("GITHUB_REF_NAME") or current_branch()


def load_flow_metadata(path: str | Path = FLOW_PATH) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.exists():
        raise ConfigError(f"Missing OmniFlow metadata file: {candidate}")
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ConfigError(f"Metadata file {candidate} must contain a JSON object")
    reject_secret_keys(payload, source=str(candidate))
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise ConfigError(f"Metadata file {candidate} must include a non-empty models list")
    return payload


def load_pr_marker() -> dict[str, Any]:
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    body = ((event.get("pull_request") or {}).get("body") or "")
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
    return payload


def get_changed_files(base_branch: str | None = None) -> list[str]:
    explicit = os.getenv("OMNIFLOW_CHANGED_FILES")
    if explicit:
        return [item.strip() for item in explicit.splitlines() if item.strip()]

    candidates = []
    if os.getenv("GITHUB_BASE_REF"):
        candidates.append(f"origin/{os.getenv('GITHUB_BASE_REF')}...HEAD")
    if base_branch:
        candidates.append(f"origin/{base_branch}...HEAD")
    candidates.extend(["HEAD~1...HEAD", "--cached"])

    for candidate in candidates:
        cmd = ["git", "diff", "--name-only"]
        cmd.append(candidate)
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
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
    models = [_model_from_payload(item, branch_name=branch_name) for item in flow["models"] if isinstance(item, dict)]
    marker = marker or {}
    if marker.get("model_id"):
        matching = [context for context in models if context.model_id == marker["model_id"]]
        if not matching:
            raise ConfigError("OmniFlow PR marker references a model_id not present in .omni/flow.json")
        for context in matching:
            context.branch_name = marker.get("branch_name") or context.branch_name
        return matching

    matched = [
        context
        for context in models
        if context.model_path and any(_is_under_model_path(path, context.model_path) for path in changed_files)
    ]
    if matched:
        return matched
    if allow_skip and changed_files:
        return []
    if allow_skip and not changed_files:
        return []
    if len(models) == 1:
        return models
    raise ConfigError("Could not select an Omni model from changed files. Add an omniflow-context PR marker.")


def _model_from_payload(payload: dict[str, Any], *, branch_name: str | None, require_model_path: bool = True) -> ModelContext:
    keys = ("base_url", "model_id", "model_path") if require_model_path else ("base_url", "model_id")
    for key in keys:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ConfigError(f"OmniFlow model context must include {key}")
    return ModelContext(
        base_url=payload["base_url"].strip(),
        model_id=payload["model_id"].strip(),
        model_path=str(payload.get("model_path") or "").strip().strip("/"),
        branch_name=branch_name,
        base_branch=payload.get("base_branch"),
        git_provider=payload.get("git_provider"),
        web_url=payload.get("web_url"),
    )


def _is_under_model_path(path: str, model_path: str) -> bool:
    normalized_path = path.strip().strip("/")
    normalized_model_path = model_path.strip().strip("/")
    return normalized_path == normalized_model_path or normalized_path.startswith(f"{normalized_model_path}/")
