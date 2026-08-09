from __future__ import annotations

import os
import re

# Git is invoked without a shell and with validated inputs.
import subprocess  # nosec B404
from pathlib import Path

from .exceptions import ConfigError
from .git import git_executable, is_pull_request_event

SAFE_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def is_github_pull_request() -> bool:
    return is_pull_request_event() and bool(os.getenv("GITHUB_BASE_REF"))


def read_trusted_repo_text(path: str | Path) -> str | None:
    candidate = Path(path)
    if candidate.is_absolute() or not is_github_pull_request():
        try:
            return candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError(f"Could not read '{candidate}': {exc}") from exc

    repo_path = _safe_repo_path(candidate)
    base_ref = _safe_base_ref(os.environ["GITHUB_BASE_REF"])
    for ref in (f"refs/remotes/origin/{base_ref}", f"refs/heads/{base_ref}"):
        try:
            # The ref and repository path are validated before this invocation.
            result = subprocess.run(  # nosec B603
                [git_executable(), "show", f"{ref}:{repo_path}"],
                check=True,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise ConfigError(f"Could not read trusted base revision for '{candidate}': {exc}") from exc
        except subprocess.CalledProcessError:
            continue
        return result.stdout
    return None


def _safe_repo_path(path: Path) -> str:
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"Trusted repository path must stay inside the checkout: {path}")
    value = path.as_posix()
    while value.startswith("./"):
        value = value[2:]
    if not value:
        raise ConfigError("Trusted repository path cannot be empty")
    return value


def _safe_base_ref(value: str) -> str:
    if (
        not SAFE_GIT_REF_RE.fullmatch(value)
        or value.startswith("-")
        or ".." in value
        or "@{" in value
        or value.endswith("/")
    ):
        raise ConfigError("GITHUB_BASE_REF contains an unsafe Git reference")
    return value
