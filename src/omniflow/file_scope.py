"""Trusted, bounded classification of repository files; never an API filter."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from .exceptions import ConfigError
from .model_paths import authored_file_identity, filename_identity

MAX_NON_OMNI_RULES = 100
MAX_PATTERN_LENGTH = 1024
MAX_REASON_LENGTH = 240
_RULE_KEYS = {"tag", "pattern", "reason"}
_TYPED_SUFFIXES = (".view", ".topic", ".composite_topic", ".relationships")
_CONTROL_FILES = {".omniflow.yml", ".omniflow.example.yml"}
_CONTROL_DIRECTORIES = {".git", ".github", ".omni", ".omniflow"}


class FileScopeError(ConfigError):
    """Safe classification failures do not contain paths or policy free text."""

    def __init__(self, reason: str) -> None:
        self.file_scope_reason = reason
        super().__init__(f"Trusted non-Omni file classification failed ({reason})")


@dataclass(frozen=True)
class NonOmniRule:
    tag: str
    pattern: str
    reason: str


def parse_non_omni_rules(value: Any) -> list[NonOmniRule]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_NON_OMNI_RULES:
        raise ConfigError("files.non_omni must be a list containing at most 100 rules")
    rules: list[NonOmniRule] = []
    tags: set[str] = set()
    patterns: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _RULE_KEYS:
            raise ConfigError("Each files.non_omni rule must contain only tag, pattern, and reason")
        tag, pattern, reason = item["tag"], item["pattern"], item["reason"]
        if not isinstance(tag, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", tag):
            raise ConfigError("files.non_omni tags must be lowercase identifiers of at most 64 characters")
        _validate_pattern(pattern)
        if (
            not isinstance(reason, str) or not reason.strip() or reason != reason.strip()
            or len(reason) > MAX_REASON_LENGTH or any(not char.isprintable() for char in reason)
        ):
            raise ConfigError("files.non_omni reasons must be nonempty single-line strings of at most 240 characters")
        if tag in tags or pattern in patterns:
            raise ConfigError("files.non_omni contains a duplicate tag or pattern")
        tags.add(tag)
        patterns.add(pattern)
        rules.append(NonOmniRule(tag, pattern, reason))
    # Detect literal/subtree overlap without inventing a precedence rule.
    # Remaining glob overlaps are rejected when an inventoried path matches.
    for index, rule in enumerate(rules):
        for other in rules[index + 1:]:
            if (
                "*" not in rule.pattern and _pattern_matches(rule.pattern, other.pattern)
                or "*" not in other.pattern and _pattern_matches(other.pattern, rule.pattern)
            ):
                raise FileScopeError("non_omni_rule_conflict")
    return rules


def _validate_pattern(value: Any) -> None:
    if (
        not isinstance(value, str) or not value or len(value) > MAX_PATTERN_LENGTH
        or value != value.strip() or value.startswith("/") or value.endswith("/")
        or any(not char.isprintable() for char in value)
        or any(char in value for char in ("\\", ":", "?", "[", "]", "{", "}", "!", "$"))
    ):
        raise ConfigError("files.non_omni patterns must be bounded repository-relative POSIX paths")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part != part.strip() for part in parts):
        raise ConfigError("files.non_omni patterns must not contain empty or relative path components")
    if any("**" in part and part != "**" for part in parts) or value.count("**") > 1:
        raise ConfigError("files.non_omni permits at most one complete ** path component")
    if "*" in parts[0] or value.endswith("**") or all(char == "*" for char in parts[-1]):
        raise ConfigError("files.non_omni glob patterns need a literal root and a bounded filename pattern")
    # An explicitly named source/control file can never be tagged as ancillary.
    if "*" not in value and (is_known_omni_source(value) or _is_control_file(value)):
        raise FileScopeError("non_omni_source_conflict")


@lru_cache(maxsize=MAX_NON_OMNI_RULES * 2)
def _pattern_parts(pattern: str) -> tuple[str, ...]:
    return tuple(pattern.split("/"))


def _pattern_matches(path: str, pattern: str) -> bool:
    parts, patterns = path.split("/"), _pattern_parts(pattern)
    if "**" not in patterns:
        return len(parts) == len(patterns) and all(
            fnmatchcase(part, glob) for part, glob in zip(parts, patterns, strict=True)
        )
    split = patterns.index("**")
    before, after = patterns[:split], patterns[split + 1:]
    return (
        len(parts) >= len(before) + len(after)
        and all(fnmatchcase(part, glob) for part, glob in zip(parts, before, strict=False))
        and all(fnmatchcase(part, glob) for part, glob in zip(parts[len(parts) - len(after):], after, strict=True))
    )


def _safe_repo_path(path: str) -> None:
    if (
        not isinstance(path, str) or not path or len(path) > MAX_PATTERN_LENGTH
        or path != path.strip() or any(not char.isprintable() for char in path)
        or "\\" in path or ":" in path or PurePosixPath(path).is_absolute()
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise FileScopeError("invalid_file_scope_path")


def matching_non_omni_rule(path: str, rules: Sequence[NonOmniRule] = ()) -> NonOmniRule | None:
    """Return classification intent, not proof that a file is absent from Omni."""
    _safe_repo_path(path)
    matches = [rule for rule in rules if _pattern_matches(path, rule.pattern)]
    if len(matches) > 1:
        raise FileScopeError("non_omni_rule_conflict")
    return matches[0] if matches else None


def is_known_omni_source(path: str) -> bool:
    """Protect typed files and reserved basenames, including native wrappers."""
    basename = PurePosixPath(path).name
    for suffix in (".yaml", ".yml"):
        if basename.endswith(suffix):
            basename = basename[:-len(suffix)]
            break
    return basename in {"model", "relationships"} or basename.endswith(_TYPED_SUFFIXES)


def _is_control_file(path: str) -> bool:
    return path in _CONTROL_FILES or path.split("/", 1)[0] in _CONTROL_DIRECTORIES


def classify_repo_file(path: str, rules: Sequence[NonOmniRule] = ()) -> str:
    """Classify a safe path without weakening other checks or consulting APIs."""
    rule = matching_non_omni_rule(path, rules)
    known_source = is_known_omni_source(path)
    if rule and (known_source or _is_control_file(path)):
        raise FileScopeError("non_omni_source_conflict")
    if known_source:
        return "omni"
    return "tagged_non_omni" if rule else "unknown"


def excluded_semantic_paths(
    git_paths: Iterable[str], api_paths: Iterable[str], *, model_path: str,
    rules: Sequence[NonOmniRule] = (),
) -> set[str]:
    """Return ancillary Git paths only; reject rules covering any API source.

    Both inventories use model-relative names. Patterns always use full
    repository-relative names. The caller must invoke this for base and head.
    """
    if not rules:
        return set()
    prefix = "" if model_path == "." else model_path + "/"
    api_identities: set[str] = set()
    for path in api_paths:
        identity = authored_file_identity(path)
        api_identities.add(filename_identity(path))
        aliases = {path, identity}
        if is_known_omni_source(identity):
            aliases.update({identity + ".yaml", identity + ".yml"})
        for alias in aliases:
            if matching_non_omni_rule(prefix + alias, rules):
                raise FileScopeError("non_omni_source_conflict")
    excluded = set()
    for path in git_paths:
        if classify_repo_file(prefix + path, rules) == "tagged_non_omni":
            if filename_identity(path) in api_identities:
                raise FileScopeError("non_omni_source_conflict")
            excluded.add(path)
    return excluded
