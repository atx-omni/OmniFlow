from __future__ import annotations

import os
import re
from pathlib import Path

from .exceptions import ConfigError
from .git import github_event_payload, is_pull_request_event

SAFE_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
MAX_TRUSTED_FILE_BYTES = 1024 * 1024


def is_github_pull_request() -> bool:
    return is_pull_request_event() and bool(os.getenv("GITHUB_BASE_REF"))


def read_trusted_repo_text(path: str | Path, *, max_bytes: int = MAX_TRUSTED_FILE_BYTES) -> str | None:
    candidate = Path(path)
    if candidate.is_absolute() or not is_github_pull_request():
        if candidate.is_symlink():
            raise ConfigError("Trusted repository file must not be a symbolic link", reason_code="trusted_file_unsafe")
        try:
            with candidate.open("rb") as stream:
                value = stream.read(max_bytes + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError("Could not read trusted repository file", reason_code="trusted_file_unavailable") from exc
        if len(value) > max_bytes:
            raise ConfigError("Trusted repository file exceeds the safety limit", reason_code="trusted_file_too_large")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError("Trusted repository file is not valid UTF-8",
                              reason_code="trusted_file_invalid_encoding") from exc

    repo_path = _safe_repo_path(candidate)
    event = github_event_payload()
    pull_request = event.get("pull_request")
    base = pull_request.get("base") if isinstance(pull_request, dict) else None
    if isinstance(base, dict) and "sha" in base:
        # Bind new exclusions and policy to the event's immutable trusted base,
        # not whichever commit a mutable main ref has advanced to meanwhile.
        from .revision_data import pull_request_revision, read_git_text, read_github_text

        revision = pull_request_revision("base")
        if revision is None:
            raise ConfigError("Trusted policy requires an immutable base revision")
        try:
            return read_git_text(revision[0], repo_path, root=Path("."), max_bytes=max_bytes)
        except ConfigError:
            # Shallow Actions checkouts may contain only the merge commit. The
            # same immutable base can be read as bounded data; never substitute
            # the checked-out PR policy or a mutable branch reference.
            return read_github_text(revision[1], revision[0], repo_path, max_bytes=max_bytes)
    base_ref = _safe_base_ref(os.environ["GITHUB_BASE_REF"])
    from .revision_data import read_git_text

    for ref in (f"refs/remotes/origin/{base_ref}", f"refs/heads/{base_ref}"):
        try:
            # None means the revision was readable and the file was absent.
            # A missing/unreadable revision is not evidence of a missing file.
            return read_git_text(ref, repo_path, root=Path("."), max_bytes=max_bytes)
        except ConfigError:
            continue
    raise ConfigError("Trusted base revision is unavailable", reason_code="trusted_file_unavailable")


def _safe_repo_path(path: Path) -> str:
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError("Trusted repository path must stay inside the checkout", reason_code="trusted_file_unsafe")
    value = path.as_posix()
    while value.startswith("./"):
        value = value[2:]
    if not value:
        raise ConfigError("Trusted repository path cannot be empty", reason_code="trusted_file_unsafe")
    if len(value) > 1_024 or any(character in value for character in ("\x00", "\r", "\n", "\\", ":")):
        raise ConfigError("Trusted repository path contains unsafe characters", reason_code="trusted_file_unsafe")
    return value


def _safe_base_ref(value: str) -> str:
    if (
        not SAFE_GIT_REF_RE.fullmatch(value)
        or value.startswith("-")
        or ".." in value
        or "@{" in value
        or value.endswith("/")
    ):
        raise ConfigError("GITHUB_BASE_REF contains an unsafe Git reference", reason_code="trusted_file_unsafe")
    return value
